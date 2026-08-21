#!/usr/bin/env python3
"""Proves the FLOOD_WAIT behaviour without Telegram, because the real thing is
unreproducible on demand — you cannot ask Telegram to rate-limit you at a
convenient moment, and "we handled it" is exactly the kind of claim that is
believed until the first client sync of a 199-chat folder.

Three properties, each of which was WRONG before this change:

  1. A wait under the cap sleeps and RETRIES THE SAME CHAT, and the chat still
     ends up complete. (Before: the chat was marked `failed` and abandoned.)
  2. A wait over the cap stops the ACCOUNT, not just the chat: chats already read
     stay committed, the unreached ones keep their watermarks, and the run is
     `flood_wait` — not `ok`, not `failed`. (Before: every remaining chat failed
     for the same account-wide reason, and the run still finished `ok`.)
  3. The out-of-scope sweep does NOT run on a flood stop. (Before: it would have
     marked every unreached chat `in_scope=0` — "the person removed it from the
     folder" — which is consent inferred from a rate limit.)

    python3 test_floodwait.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import types as pytypes
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Telethon and the read-only barrier are not needed to exercise the control flow,
# and requiring them would make this test skip exactly where it matters most.
if "telethon" not in sys.modules:
    tl = pytypes.ModuleType("telethon")
    errors = pytypes.ModuleType("telethon.errors")
    tl_tl = pytypes.ModuleType("telethon.tl")
    fns = pytypes.ModuleType("telethon.tl.functions")
    tps = pytypes.ModuleType("telethon.tl.types")
    sess = pytypes.ModuleType("telethon.sessions")

    class FloodWaitError(Exception):
        def __init__(self, seconds):
            super().__init__(f"flood {seconds}")
            self.seconds = seconds

    class _U:  pass
    class _C:  pass
    class _Ch: pass

    # A real class, not `object`: this machine carries a site-wide import hook
    # (`tg_guard_boot`) that annotates TelegramClient when telethon is imported,
    # and it cannot set an attribute on an immutable builtin. Fighting that guard
    # would be the wrong instinct — it is the same class of protection as our own
    # read-only barrier.
    class _Client:  pass

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
from telethon.errors import FloodWaitError                            # noqa: E402


class Msg:
    def __init__(self, mid):
        self.id, self.message, self.date = mid, f"m{mid}", datetime.now(timezone.utc)
        self.out = False
        self.sender_id, self.reply_to, self.fwd_from, self.edit_date = 1, None, None, None
        self.media = None


class Ent:
    def __init__(self, cid):
        self.id, self.access_hash, self.title, self.username = cid, 1, f"chat{cid}", None
        self.first_name = self.last_name = ""


class FakeClient:
    """Raises FLOOD_WAIT on a chosen chat, a chosen number of times."""

    def __init__(self, chats, flood_on, flood_secs, flood_times):
        self.chats, self.flood_on, self.flood_secs = chats, flood_on, flood_secs
        self.left = flood_times
        self.walked, self.flood_sleep_threshold = [], 60

    async def connect(self):            return None
    async def is_user_authorized(self):  return True
    async def disconnect(self):          return None
    async def get_entity(self, peer):    return Ent(peer)

    def iter_messages(self, entity, **kw):
        cid = entity.id
        client = self

        class It:
            def __aiter__(self):
                self.i = 0
                return self

            async def __anext__(self):
                if cid == client.flood_on and client.left > 0:
                    client.left -= 1
                    raise FloodWaitError(client.flood_secs)
                if self.i >= 2:
                    raise StopAsyncIteration
                self.i += 1
                client.walked.append(cid)
                return Msg(self.i)
        return It()


def _db():
    db = sqlite3.connect(":memory:")
    db.executescript((HERE / "schema.sql").read_text())
    db.execute("INSERT INTO accounts(slug, folder_title, active, added_at) VALUES('t','F',1,?)",
               (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    db.commit()
    return db


async def run(flood_on, secs, times, cap):
    db = _db()
    cl = FakeClient([1, 2, 3], flood_on, secs, times)
    tg_sync.folder_peers = lambda client, folder: _peers()
    orig_sleep = asyncio.sleep
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        await orig_sleep(0)
    asyncio.sleep = fake_sleep
    try:
        await tg_sync.sync_account_with(db, "t", "F", cl, full=False, per_chat_limit=2,
                                        backfill_pages=0, flood_cap=cap)
    finally:
        asyncio.sleep = orig_sleep
    return db, cl, slept


async def _peers():
    return [1, 2, 3]


def q1(db, sql, *a):
    r = db.execute(sql, a).fetchone()
    return r[0] if r else None


async def main():
    ok = True

    # 1. short wait -> retried, chat completes, run is ok
    db, cl, slept = await run(flood_on=2, secs=30, times=1, cap=900)
    st = q1(db, "SELECT status FROM sync_runs")
    n2 = q1(db, "SELECT count(*) FROM messages WHERE chat_id=2")
    print(f"1) короткий флуд: run={st!r} · спали {slept} · сообщений в чате 2 = {n2}")
    ok &= st == "ok" and n2 == 2 and slept == [32]

    # 2. long wait -> account stops, earlier chat kept, run is flood_wait
    db, cl, slept = await run(flood_on=2, secs=4000, times=99, cap=900)
    st = q1(db, "SELECT status FROM sync_runs")
    err = q1(db, "SELECT error FROM sync_runs")
    n1 = q1(db, "SELECT count(*) FROM messages WHERE chat_id=1")
    n3 = q1(db, "SELECT count(*) FROM messages WHERE chat_id=3")
    ws = q1(db, "SELECT count(*) FROM sync_state WHERE chat_id=3")
    print(f"2) длинный флуд: run={st!r} · error={err!r}")
    print(f"   чат 1 прочитан ({n1} сообщ.) · чат 3 не тронут ({n3} сообщ., строк состояния {ws})")
    ok &= st == "flood_wait" and n1 == 2 and n3 == 0 and ws == 0 and "4000" in (err or "")

    # 3. the out-of-scope sweep must NOT have run
    scoped = q1(db, "SELECT count(*) FROM chats WHERE in_scope=1")
    print(f"3) чаты остались в области: {scoped} (снятия по флуду быть не должно)")
    ok &= scoped >= 1

    print("\n%s" % ("FLOOD-WAIT SELFTEST: OK" if ok else "FLOOD-WAIT SELFTEST: ПРОВАЛ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
