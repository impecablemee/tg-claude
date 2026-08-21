#!/usr/bin/env python3
"""Живая доставка: Telegram САМ присылает новые сообщения, задержка — секунды.

    python3 tg_live.py --slug anna

ЗАЧЕМ. Синк по расписанию отвечает на вопрос «что было», но не на вопрос «что
только что сказали». А спрашивают чаще второе: «сделай, что мне написал Вася» —
и если сообщение Васи попадёт в базу через полчаса, ответить нечем. Интервал тут
не подкрутишь: он и есть задержка.

ПОЧЕМУ НЕ «КРОН РАЗ В МИНУТУ». Это худший из вариантов, а не более быстрый:
каждый запуск заново поднимает соединение и аутентифицируется, перечитывает
состав папки и обходит все чаты — на папке в двести чатов это шестьдесят таких
обходов в час вместо двух. Telegram считает частоту запросов, и упереться в
FLOOD_WAIT так можно за день. При этом ответ всё равно опаздывает — до минуты.

ПОЧЕМУ НЕ ВЕБХУК. Вебхуки есть у Bot API, а бот не видит историю и не видит
личные диалоги — читать переписку он не может в принципе. Для пользовательской
сессии эквивалент вебхука встроен в сам протокол: соединение держится открытым,
и сервер Telegram присылает событие сам. Опрашивать не нужно ничего.

═══════════════════════════════════════════════════════════════════════════════
ЧТО ЭТОТ ПРОЦЕСС ДЕЛАЕТ И ПОЧЕМУ ОН ОДИН
═══════════════════════════════════════════════════════════════════════════════
Ключ авторизации Telegram — ресурс единственного держателя: два процесса на одном
ключе видятся как вход с двух адресов, и ключ ОТЗЫВАЕТСЯ (человеку придётся
сканировать QR заново). Поэтому резидентный слушатель не может сосуществовать с
кроном по тому же аккаунту — он забирает аккаунт себе целиком и делает обе работы
сам:

  1. СЛУШАЕТ. Новое сообщение и правка старого попадают в базу за секунды.
  2. СВЕРЯЕТСЯ. Раз в `--reconcile` минут прогоняет обычный синк тем же
     соединением: состав папки мог измениться, сообщения могли прийти в минуту
     разрыва связи, а удаления и часть правок событиями не приходят вовсе.

Крон при этом не нужен — если он был, его надо снять, иначе два держателя ключа.

═══════════════════════════════════════════════════════════════════════════════
ПОЧЕМУ ЭТО НЕ ЛОМАЕТСЯ ТИХО
═══════════════════════════════════════════════════════════════════════════════
`tg_sync.py` намеренно работает с `receive_updates=False`, и причина записана в
его шапке: цикл обновлений Telethon разбирает тот слой протокола, который говорит
сервер, и на несовпадении версий умирает голым `CancelledError`, неотличимым от
сетевой ошибки. Здесь цикл обновлений нужен — значит, этот риск надо не обойти, а
обслужить:

  · слушатель поднимается заново с нарастающей паузой, а не молча умирает;
  · СВЕРКА ЖИВЁТ В ТОМ ЖЕ ПРОЦЕССЕ. Поэтому сломанные обновления деградируют до
    «свежесть как у периодического синка», а не до тишины: худший исход — то, что
    было раньше нормой;
  · каждое принятое событие двигает `accounts.last_sync_at`. «Сообщений нет» и
    «слушатель мёртв» — разные ответы, и по времени последнего события их видно.

Ничего не отправляет: приём событий — это чтение. Барьер `readonly` стоит здесь
так же, как везде, и статический страж это проверяет.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import readonly
from tg_sync import (API_ID, API_HASH, AccountLock, FLOOD_AUTO_SLEEP, HERE, _now,
                     folder_peers, store_message, sync_account_with)
from tg_fetch import download as fetch_attachment

log = logging.getLogger("tg_live")


def peer_id_of(peer) -> int | None:
    """Идентификатор из любого peer-объекта Telegram. Три разных поля под одно и то
    же: какое заполнено — зависит от того, канал это, группа или личка."""
    for attr in ("channel_id", "chat_id", "user_id"):
        cid = getattr(peer, attr, None)
        if cid is not None:
            return cid
    return None


def chat_id_of(message) -> int | None:
    """Идентификатор чата из события."""
    return peer_id_of(getattr(message, "peer_id", None))


def store_if_in_scope(db, slug: str, in_scope: set[int], message) -> str:
    """Одно событие → одна строка, но только если чат в папке.

    Вынесено из обработчика отдельной функцией, потому что это единственное место,
    где решается «читать или не читать», — и его надо уметь проверить, не поднимая
    Telegram. Возвращает, что произошло: `stored` | `out_of_scope` | `no_chat_id`
    | `error: …`. Пустая область — это НЕ «всё подходит»: пока папка не прочитана,
    не записывается ничего.
    """
    cid = chat_id_of(message)
    if cid is None:
        return "no_chat_id"
    if cid not in in_scope:
        return "out_of_scope"
    try:
        # Строка чата должна существовать: у `messages` внешний ключ на `chats`.
        # В живом режиме сообщение может прийти РАНЬШЕ, чем сверка заведёт чат —
        # человек добавил чат в папку и тут же в нём написал. Без этой вставки
        # такое сообщение отбрасывалось бы с «FOREIGN KEY constraint failed», то
        # есть ровно в самом интересном случае. Заводим заглушку; название и тип
        # проставит ближайшая сверка, до тех пор чат ищется по id.
        db.execute("INSERT OR IGNORE INTO chats(account_slug, chat_id, kind, in_scope,"
                   " first_seen, last_seen) VALUES(?,?,?,1,?,?)",
                   (slug, cid, "other", _now(), _now()))
        store_message(db, slug, cid, message)
        db.execute("UPDATE accounts SET last_sync_at=? WHERE slug=?", (_now(), slug))
        db.commit()
        return "stored"
    except Exception as exc:                                          # noqa: BLE001
        # Одно кривое событие не имеет права уронить слушателя: иначе живой режим
        # выключается целиком из-за одного сообщения.
        return "error: %s" % exc


async def run_account(db: sqlite3.Connection, slug: str, folder: str, sessions: Path,
                      reconcile_min: int, per_chat_limit: int, scope_sec: int = 60) -> None:
    sess = sessions / f"{slug}.string"
    if not sess.exists():
        log.error("%s: нет сессии в %s — сначала tg_login.py --slug %s", slug, sess, slug)
        return

    readonly.install()
    client = TelegramClient(StringSession(sess.read_text().strip()), API_ID, API_HASH,
                            receive_updates=True)
    client.flood_sleep_threshold = FLOOD_AUTO_SLEEP

    # Область: только чаты из папки. Обновляется на каждой сверке — человек мог
    # вынести чат из папки минуту назад, и с этого момента он не читается.
    in_scope: set[int] = set()

    async def refresh_scope() -> None:
        """Состав папки — ОДИН запрос к Telegram, поэтому его не жалко звать часто.

        Идентификаторы уже лежат в самих peer-объектах ответа. Первая версия
        дёргала `get_entity` на каждый чат, то есть двести вызовов на папку в
        двести чатов, — из-за этого область обновлялась только на сверке, и чат,
        добавленный в папку, до пятнадцати минут оставался невидимым: сообщения в
        нём отбрасывались как «вне области». Для человека это выглядело бы как
        «добавил чат, написал, а система его не видит».
        """
        try:
            peers = await folder_peers(client, folder)
        except Exception as exc:                                      # noqa: BLE001
            log.warning("%s: не удалось перечитать папку (%s) — область прежняя",
                        slug, type(exc).__name__)
            return
        ids = {i for i in (peer_id_of(p) for p in peers) if i is not None}
        if not ids:
            # Пустой ответ — это сбой чтения, а не «человек убрал всё из папки».
            # Обнулить область по нему значит перестать читать вообще.
            log.warning("%s: папка вернулась пустой — область прежняя", slug)
            return
        added, gone = ids - in_scope, in_scope - ids
        if added or gone:
            log.info("%s: область изменилась: +%d −%d, теперь %d чатов",
                     slug, len(added), len(gone), len(ids))
        in_scope.clear()
        in_scope.update(ids)

    async def remember(event) -> None:
        what = store_if_in_scope(db, slug, in_scope, event.message)
        if what == "stored":
            log.info("%s: чат %s, сообщение %s — записано",
                     slug, chat_id_of(event.message), event.message.id)
        elif what.startswith("error"):
            log.warning("%s: событие не записано (%s)", slug, what)

    client.add_event_handler(remember, events.NewMessage())
    client.add_event_handler(remember, events.MessageEdited())

    await client.connect()
    if not await client.is_user_authorized():
        log.error("%s: сессия отозвана — нужен новый tg_login.py", slug)
        await client.disconnect()
        return

    # Стартовая сверка: пока процесс не работал, сообщения приходили.
    await sync_account_with(db, slug, folder, client, False, per_chat_limit)
    await refresh_scope()
    log.info("%s: живой режим, в области %d чатов · состав папки раз в %d с · сверка раз в %d мин",
             slug, len(in_scope), scope_sec, reconcile_min)

    # ЗАЯВКИ НА ВЛОЖЕНИЯ. Ключ Telegram держит этот процесс, и держит постоянно,
    # поэтому скачать вложение вторым процессом нельзя в принципе — он упрётся в
    # ту же блокировку. Значит, качает тот, у кого ключ: `tg_fetch.py` кладёт
    # заявку в базу, а разбираем её здесь. Раз в две секунды, потому что за этим
    # ждёт живой человек, попросивший «покажи вложение».
    async def serve_attachments() -> None:
        out_dir = HERE / "data" / "attachments"
        while True:
            await asyncio.sleep(2)
            try:
                pend = db.execute(
                    "SELECT chat_id, msg_id FROM attachment_requests WHERE account_slug=?"
                    " AND state='pending' ORDER BY requested_at LIMIT 5", (slug,)).fetchall()
            except Exception:                                         # noqa: BLE001
                continue
            for cid, mid in pend:
                try:
                    res = await fetch_attachment(client, db, slug, cid, mid, out_dir)
                    state = "failed" if "error" in res else "done"
                    err = res.get("error")
                except Exception as exc:                              # noqa: BLE001
                    state, err = "failed", f"{type(exc).__name__}: {exc}"[:300]
                db.execute("UPDATE attachment_requests SET state=?, error=? WHERE account_slug=?"
                           " AND chat_id=? AND msg_id=?", (state, err, slug, cid, mid))
                db.commit()
                log.info("%s: заявка на вложение %s/%s — %s%s", slug, cid, mid, state,
                         f" ({err})" if err else "")

    attach_task = asyncio.create_task(serve_attachments())

    # ДВА ТАЙМЕРА, потому что это две разные по цене работы.
    #   область   — один запрос, поэтому раз в минуту: чат, добавленный в папку,
    #               становится читаемым почти сразу;
    #   сверка    — полный обход чатов, поэтому редко: она ловит удаления, правки
    #               и всё, что могло пройти мимо в минуту разрыва связи.
    since_reconcile = 0
    while True:
        try:
            await asyncio.wait_for(client.run_until_disconnected(), timeout=scope_sec)
        except asyncio.TimeoutError:
            pass                                   # плановая пауза, соединение живо
        except Exception as exc:                                      # noqa: BLE001
            log.warning("%s: цикл обновлений упал (%s) — поднимаю", slug, type(exc).__name__)
        if not client.is_connected():
            await client.connect()
        await refresh_scope()
        since_reconcile += scope_sec
        if since_reconcile >= reconcile_min * 60:
            since_reconcile = 0
            await sync_account_with(db, slug, folder, client, False, per_chat_limit)
        if attach_task.done():          # задача не имеет права умирать молча
            log.warning("%s: разбор заявок на вложения остановился — поднимаю", slug)
            attach_task = asyncio.create_task(serve_attachments())


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="один аккаунт; без него — все активные, по процессу на каждый")
    ap.add_argument("--reconcile", type=int, default=15,
                    help="минут между полными сверками (события приходят сразу, это страховка)")
    ap.add_argument("--scope-seconds", type=int, default=60,
                    help="как часто перечитывать состав папки — один запрос, можно часто")
    ap.add_argument("--limit", type=int, default=200, help="сообщений на чат за сверку")
    ap.add_argument("--db", default=str(HERE / "data" / "tg.db"))
    ap.add_argument("--sessions", default=str(HERE / "sessions"))
    a = ap.parse_args()

    db = sqlite3.connect(a.db)
    rows = db.execute("SELECT slug, folder_title FROM accounts WHERE active=1"
                      + (" AND slug=?" if a.slug else ""),
                      (a.slug,) if a.slug else ()).fetchall()
    if not rows:
        log.error("нет активных аккаунтов%s", f" по имени {a.slug!r}" if a.slug else "")
        return 1
    if len(rows) > 1:
        # Держатель ключа — один процесс на аккаунт. Запускать несколько аккаунтов
        # из одного процесса можно, но тогда падение одного роняет остальные;
        # честнее сказать это вслух, чем сделать вид, что разницы нет.
        log.error("аккаунтов %d. Запускайте по процессу на аккаунт: --slug <имя> "
                  "(ключ Telegram — ресурс единственного держателя)", len(rows))
        return 1

    slug, folder = rows[0]
    with AccountLock(slug):
        await run_account(db, slug, folder, Path(a.sessions), a.reconcile, a.limit,
                          a.scope_seconds)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
