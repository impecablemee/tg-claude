#!/usr/bin/env python3
"""Проверяет, что состав папки перечитывается своим таймером — без Telegram.

Живой режим пишет сообщение, только если чат в области, а область берётся из
папки. Пока она обновлялась вместе с полной сверкой, обе стороны границы
отставали на четверть часа, и отставали в самый заметный момент — в первую
минуту работы с системой:

  · чат ВНЕСЛИ в папку — события из него отбрасывались как чужие. «Добавил чат,
    написал в него, система его не видит»;
  · чат ВЫНЕСЛИ из папки — его продолжали читать. Это хуже первого: согласие
    отозвано, а чтение шло.

Ускорить это уменьшением `--reconcile` нельзя — сверка обходит КАЖДЫЙ чат и в
частом режиме упрётся в FLOOD_WAIT. Поэтому таймеров два, и состав папки стоит
ОДИН запрос: идентификаторы лежат в самих peer-объектах ответа. Четвёртая
проверка сторожит именно эту цену — если кто-то снова начнёт разрешать сущности
по чату, частый таймер сам станет источником ограничения, то есть защита области
выключит область.

    python3 test_live_rescan.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tg_live                                                        # noqa: E402

SCOPE_SEC = 1        # секунда, чтобы тест шёл секунды, а не минуты
RECONCILE_MIN = 1    # одно окно сверки = 60 с; весь тест живёт внутри него


class Peer:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class Msg:
    def __init__(self, mid, peer, text="привет"):
        self.id, self.peer_id, self.message = mid, peer, text
        self.date = datetime.now(timezone.utc)
        self.out, self.sender_id = False, 7
        self.reply_to = self.fwd_from = self.edit_date = self.media = None


class FakeClient:
    """Ровно та поверхность, которой пользуется run_account."""

    flood_sleep_threshold = 0

    def __init__(self):
        self.handlers = []
        self.entity_calls = 0

    async def connect(self): pass
    async def disconnect(self): pass
    def is_connected(self): return True
    async def is_user_authorized(self): return True
    def add_event_handler(self, cb, _event): self.handlers.append(cb)
    async def run_until_disconnected(self): await asyncio.sleep(3600)

    async def get_entity(self, _p):                 # звать не должны — см. проверку 4
        self.entity_calls += 1
        return SimpleNamespace(id=0)


async def scenario() -> int:
    folder = [Peer(channel_id=100), Peer(channel_id=101)]
    seen = {"folder": 0, "reconcile": 0}
    client = FakeClient()

    async def folder_peers(_client, _title):
        seen["folder"] += 1
        return list(folder)

    async def reconcile(*_a, **_k):
        seen["reconcile"] += 1

    tg_live.folder_peers = folder_peers
    tg_live.sync_account_with = reconcile
    tg_live.TelegramClient = lambda *a, **k: client

    tmp = Path(tempfile.mkdtemp())
    # Пустая строка — ВАЛИДНАЯ сессия Telethon (`StringSession().save()` даёт
    # именно её), а "x" не проходит разбор: `ValueError: Not a valid string`.
    # Клиент ниже подменён, но аргумент к нему вычисляется раньше подмены, так
    # что строка обязана быть разбираемой.
    (tmp / "t.string").write_text("")
    db = sqlite3.connect(":memory:")
    db.executescript((HERE / "schema.sql").read_text())
    db.execute("INSERT INTO accounts(slug, folder_title, active, added_at) VALUES('t','F',1,?)",
               (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    db.commit()

    task = asyncio.create_task(
        tg_live.run_account(db, "t", "F", tmp, RECONCILE_MIN, 10, SCOPE_SEC))
    await asyncio.sleep(SCOPE_SEC * 1.5)

    async def fire(mid, cid):
        for cb in client.handlers[:1]:
            await cb(SimpleNamespace(message=Msg(mid, Peer(channel_id=cid))))

    def stored(mid) -> int:
        return db.execute("SELECT COUNT(*) FROM messages WHERE msg_id=?", (mid,)).fetchone()[0]

    ok = True

    await fire(1, 102)
    print("1) чата ещё нет в папке      -> сообщений: %d (ждём 0)" % stored(1))
    ok &= stored(1) == 0

    folder.append(Peer(channel_id=102))
    await asyncio.sleep(SCOPE_SEC * 2)
    await fire(2, 102)
    print("2) чат добавлен в папку      -> сообщений: %d (ждём 1, сверки не ждали)" % stored(2))
    ok &= stored(2) == 1

    folder[:] = [p for p in folder if p.channel_id != 102]
    await asyncio.sleep(SCOPE_SEC * 2)
    await fire(3, 102)
    print("3) чат вынесен из папки      -> сообщений: %d (ждём 0)" % stored(3))
    ok &= stored(3) == 0

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print("4) папка перечитана %d раз(а) при %d сверке(ах); get_entity вызван %d раз (ждём 0)"
          % (seen["folder"], seen["reconcile"], client.entity_calls))
    ok &= seen["folder"] >= 4 and seen["reconcile"] <= 1 and client.entity_calls == 0
    return 0 if ok else 1


def main() -> int:
    rc = asyncio.run(scenario())
    print("\nLIVE RESCAN: " + ("OK" if rc == 0 else "FAILED"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
