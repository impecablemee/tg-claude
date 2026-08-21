#!/usr/bin/env python3
"""Два свойства сверки, которые ломались молча. Без Telegram.

1. ЧАТ, КОТОРЫЙ СЕГОДНЯ НЕ РАЗРЕШИЛСЯ, ОСТАЁТСЯ В ОБЛАСТИ.
   В конце прогона метла снимает с области всё, чего не было в папке. Пир, на
   котором вендор ответил ошибкой (сеть моргнула, аккаунт временно ограничен),
   в список не попадал — и метла убирала чат ровно так же, как если бы человек
   вынес его из папки. Про FLOOD_WAIT это в коде уже написано; для обычной
   ошибки было неверно. Согласие нельзя выводить из технической ошибки: тихо
   переставшая читаться переписка выглядит как «там ничего не пишут».

2. ХОД ВПЕРЁД НЕ ОБРЫВАЕТСЯ ПОРОГОМ ВОЗРАСТА.
   Порог `TG_FIRST_SYNC_DAYS` отвечает на вопрос «как глубоко копать назад».
   Он применялся и к ходу вперёд — от нашей же отметки. Чат, до которого руки не
   доходили дольше срока хранения, догонялся по ОДНОМУ сообщению за прогон:
   каждый ход упирался в первое же старое сообщение и останавливался целиком,
   вместе со всем свежим, что шло за ним.

    python3 test_scope_and_catchup.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import types as pytypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

if "telethon" not in sys.modules:                  # см. шапку test_floodwait.py
    tl = pytypes.ModuleType("telethon")
    errors = pytypes.ModuleType("telethon.errors")
    tl_tl = pytypes.ModuleType("telethon.tl")
    fns = pytypes.ModuleType("telethon.tl.functions")
    tps = pytypes.ModuleType("telethon.tl.types")
    sess = pytypes.ModuleType("telethon.sessions")

    class FloodWaitError(Exception):
        def __init__(self, seconds):
            super().__init__("flood"); self.seconds = seconds

    class _U: pass
    class _C: pass
    class _Ch: pass
    class _Client: pass

    errors.FloodWaitError = FloodWaitError
    tps.User, tps.Chat, tps.Channel = _U, _C, _Ch
    fns.messages = pytypes.SimpleNamespace(GetDialogFiltersRequest=lambda: None)
    sess.StringSession = lambda s=None: s
    tl.TelegramClient = _Client
    tl.errors, tl.tl, tl.sessions = errors, tl_tl, sess
    tl_tl.functions, tl_tl.types = fns, tps
    for name, mod in (("telethon", tl), ("telethon.errors", errors), ("telethon.tl", tl_tl),
                      ("telethon.tl.functions", fns), ("telethon.tl.types", tps),
                      ("telethon.sessions", sess)):
        sys.modules[name] = mod
    sys.modules.setdefault("readonly", pytypes.SimpleNamespace(install=lambda: None))

import tg_sync                                                        # noqa: E402

NOW = datetime.now(timezone.utc)


class Peer:
    def __init__(self, cid): self.channel_id = cid


class Ent:
    def __init__(self, cid):
        self.id, self.access_hash, self.title, self.username = cid, 1, f"chat{cid}", None
        self.first_name = self.last_name = ""


class Msg:
    def __init__(self, mid, age_days):
        self.id, self.message = mid, f"m{mid}"
        self.date = NOW - timedelta(days=age_days)
        self.out, self.sender_id = False, 1
        self.reply_to = self.fwd_from = self.edit_date = self.media = None


def db_with_account():
    db = sqlite3.connect(":memory:")
    db.executescript((HERE / "schema.sql").read_text())
    db.execute("INSERT INTO accounts(slug, folder_title, active, added_at) VALUES('t','F',1,?)",
               (NOW.isoformat(timespec="seconds"),))
    db.commit()
    return db


class Client:
    """Разрешает всё, кроме перечисленного в `broken`; отдаёт заданные сообщения."""

    flood_sleep_threshold = 60

    def __init__(self, messages, broken=()):
        self.messages, self.broken = messages, set(broken)

    async def connect(self): return None
    async def is_user_authorized(self): return True
    async def disconnect(self): return None

    async def get_entity(self, peer):
        cid = peer.channel_id
        if cid in self.broken:
            raise RuntimeError("вендор ответил ошибкой на этот чат")
        return Ent(cid)

    def iter_messages(self, entity, **kw):
        items = [m for m in self.messages.get(entity.id, [])
                 if m.id > (kw.get("min_id") or 0)]
        if not kw.get("reverse"):
            items = sorted(items, key=lambda m: -m.id)
        limit = kw.get("limit") or 100

        class It:
            def __aiter__(self): self.i = 0; return self
            async def __anext__(self):
                if self.i >= min(limit, len(items)):
                    raise StopAsyncIteration
                m = items[self.i]; self.i += 1
                return m
        return It()


async def case_scope_survives_error() -> bool:
    db = db_with_account()
    db.execute("INSERT INTO chats(account_slug, chat_id, kind, title, in_scope, first_seen,"
               " last_seen) VALUES('t',200,'group','упавший',1,?,?)",
               (NOW.isoformat(), NOW.isoformat()))
    db.commit()
    tg_sync.folder_peers = lambda c, f: _peers([100, 200])
    cl = Client({100: [Msg(1, 1)]}, broken={200})
    await tg_sync.sync_account_with(db, "t", "F", cl, full=False, per_chat_limit=50)
    still = db.execute("SELECT in_scope FROM chats WHERE chat_id=200").fetchone()[0]
    print("1) чат, который вендор не отдал -> in_scope=%d (ждём 1)" % still)
    return still == 1


async def case_forward_walk_not_cut() -> bool:
    db = db_with_account()
    db.execute("INSERT INTO chats(account_slug, chat_id, kind, title, in_scope, first_seen,"
               " last_seen) VALUES('t',300,'group','тихий',1,?,?)",
               (NOW.isoformat(), NOW.isoformat()))
    # отметка стоит, а всё, что после неё, старше срока хранения
    db.execute("INSERT INTO sync_state(account_slug, chat_id, last_msg_id, oldest_msg_id,"
               " backfill_done, last_run_at, status) VALUES('t',300,5,1,1,?,'ok')",
               (NOW.isoformat(),))
    db.commit()
    old = [Msg(i, tg_sync.FIRST_SYNC_DAYS + 10) for i in range(6, 16)]
    tg_sync.folder_peers = lambda c, f: _peers([300])
    cl = Client({300: old})
    await tg_sync.sync_account_with(db, "t", "F", cl, full=False, per_chat_limit=50)
    n = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=300").fetchone()[0]
    print("2) догон чата, молчавшего дольше срока хранения -> сообщений %d за один прогон"
          " (ждём 10)" % n)
    return n == 10


async def _peers(ids):
    return [Peer(i) for i in ids]


async def main() -> int:
    ok = await case_scope_survives_error()
    ok &= await case_forward_walk_not_cut()
    print("\nSCOPE & CATCH-UP: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
