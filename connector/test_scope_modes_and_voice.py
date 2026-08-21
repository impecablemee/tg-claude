#!/usr/bin/env python3
"""Два обещания, данные заказчику вслух, — проверяются здесь. Без Telegram.

  «у меня все рабочие переписки в определённой папке — ты можешь эту настройку
   убрать, чтобы он читал скоуп того, кого ты хочешь»
  «голосовуху отправил кому-то, или фотки, или вложения — говоришь: иди
   разберись в чатике таком-то»

Из первого следует, что область НАСТРАИВАЕТСЯ: папка — режим по умолчанию и
граница согласия, но весь аккаунт тоже должен быть выбираемым — осознанно и
владельцем аккаунта, а не молчаливым запасным вариантом.

Из второго — что голосовое обязано иметь текст. Пока расшифровки не было,
инструмент отдавал `[voice]` без содержания, и для читателя это неотличимо от
пустого сообщения. А когда расшифровка невозможна, это тоже факт, и он должен
называться, иначе «нет текста» снова читается как «ничего не сказали».

Четыре свойства:
  1. область по имени папки — как раньше;
  2. область "*" — весь аккаунт, и пустой список диалогов при этом ОШИБКА, а не
     «читать нечего»;
  3. расшифрованное голосовое отдаётся с текстом;
  4. нерасшифрованное голосовое отдаётся с ОБЪЯСНЕНИЕМ, а не молча.

    python3 test_scope_modes_and_voice.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DB = Path(tempfile.mkdtemp()) / "tg.db"
os.environ["TG_DB"] = str(DB)

import tg_sync                                                        # noqa: E402

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


class Client:
    def __init__(self, dialogs=(), filters=()):
        self._dialogs, self._filters = list(dialogs), list(filters)

    async def get_dialogs(self):
        return self._dialogs

    async def __call__(self, request):
        return SimpleNamespace(filters=self._filters)


def build() -> None:
    con = sqlite3.connect(DB)
    con.executescript((HERE / "schema.sql").read_text())
    con.execute("INSERT INTO accounts(slug, folder_title, added_at, active)"
                " VALUES('t','Work',?,1)", (NOW,))
    con.execute("INSERT INTO chats(account_slug, chat_id, kind, title, in_scope, first_seen,"
                " last_seen) VALUES('t',900,'group','чат',1,?,?)", (NOW, NOW))
    for mid, mt in ((1, "voice"), (2, "voice")):
        con.execute("INSERT INTO messages(account_slug, chat_id, msg_id, date, out, sender_id,"
                    " text, media_type) VALUES('t',900,?,?,0,7,'',?)", (mid, NOW, mt))
    con.execute("INSERT INTO transcripts VALUES('t',900,1,'telegram_premium','договорились на"
                " понедельник',?)", (NOW,))
    con.commit(); con.close()


build()
import mcp_server as M                                                # noqa: E402


def call(tool, **kw):
    fn = getattr(tool, "fn", getattr(tool, "__wrapped__", tool))
    return json.loads(fn(**kw))


def main() -> int:
    ok = True

    folders = [SimpleNamespace(title="Work", pinned_peers=[SimpleNamespace(channel_id=1)],
                               include_peers=[SimpleNamespace(channel_id=2)])]
    got = asyncio.run(tg_sync.folder_peers(Client(filters=folders), "Work"))
    print("1) область по папке -> чатов %d (ждём 2)" % len(got))
    ok &= len(got) == 2

    whole = [SimpleNamespace(input_entity=SimpleNamespace(channel_id=i)) for i in (1, 2, 3)]
    got = asyncio.run(tg_sync.folder_peers(Client(dialogs=whole), "*"))
    print("2) область «*» -> чатов %d (ждём 3)" % len(got))
    ok &= len(got) == 3
    try:
        asyncio.run(tg_sync.folder_peers(Client(dialogs=[]), "*"))
        print("   пустой список диалогов принят как область — ЭТО ОШИБКА"); ok = False
    except SystemExit:
        print("   пустой список диалогов -> отказ, а не «читать нечего»")

    r = call(M.tg_history, chat="900", account="t")
    texts = {m["msg_id"]: m["transcript"] for m in r["messages"]}
    print("3) расшифрованное голосовое -> %r" % (texts.get(1) or "")[:34])
    ok &= bool(texts.get(1))

    print("4) нерасшифрованное: без текста %s, объяснение: %s"
          % (r.get("voice_without_transcript"), (r.get("note_voice") or "")[:58]))
    ok &= r.get("voice_without_transcript") == 1 and bool(r.get("note_voice"))

    acc = call(M.tg_accounts)["accounts"][0]
    print("   в tg_accounts: область %r, расшифровка %r"
          % (acc["folder_title"], acc["voice_transcription"]))
    ok &= "voice_transcription" in acc

    print("\nSCOPE MODES & VOICE: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
