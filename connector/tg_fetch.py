#!/usr/bin/env python3
"""Fetch ONE attachment, then exit. Called by mcp_server.py; never resident.

Short-lived on purpose: it takes the same per-account flock as tg_sync.py, so a
Claude session asking for a PDF can never end up holding the Telegram key while
the sync job wants it. Prints one JSON line.

ДВА ПУТИ, потому что ключ Telegram — ресурс единственного держателя:

  блокировка свободна  — качаем прямо здесь (ноутбучный вариант: живого сервиса
                         нет, аккаунт ничем не занят);
  блокировка занята    — качать НЕЛЬЗЯ, и ждать бессмысленно: живой процесс
                         держит ключ постоянно, а не «пока идёт синк». Тогда
                         кладём ЗАЯВКУ, её выполняет тот, у кого ключ, а мы
                         недолго ждём появления файла.

Пока второго пути не было, «скачай вложение» на развёрнутой системе не работало
НИКОГДА: инструмент отвечал «аккаунт занят другим синком», и это читалось как
временная занятость.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

import readonly
from tg_sync import AccountLock, API_ID, API_HASH, HERE, input_peer


async def download(client, db: sqlite3.Connection, slug: str, chat: int, msg_id: int,
                   out_dir: Path) -> dict:
    """Скачать одно вложение уже открытым клиентом и записать факт. Общий код для
    прямого пути и для живого сервиса, который разбирает заявки."""
    meta = db.execute("SELECT kind, access_hash FROM chats WHERE account_slug=? AND chat_id=?",
                      (slug, chat)).fetchone()
    if not meta:
        return {"error": f"chat {chat} not synced for {slug}"}
    peer = input_peer(meta[0], chat, meta[1])
    m = await client.get_messages(peer, ids=msg_id)
    if m is None or not m.media:
        return {"error": "no media on that message"}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = await m.download_media(file=str(out_dir / f"{slug}_{chat}_{msg_id}"))
    if not path:
        # Telethon возвращает None, если качать нечего. Без этой проверки ниже
        # падает os.stat(None) — трассировкой вместо объяснения.
        return {"error": "telegram returned no file for that media"}
    st = os.stat(path)
    mime = getattr(getattr(m, "file", None), "mime_type", None)
    db.execute("INSERT OR REPLACE INTO attachments VALUES(?,?,?,?,?,?,?)",
               (slug, chat, msg_id, str(path), mime, st.st_size,
                datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.commit()
    return {"cached": False, "path": str(path), "size": st.st_size, "mime": mime}


def _enqueue_and_wait(db: sqlite3.Connection, slug: str, chat: int, msg_id: int,
                      seconds: int) -> dict:
    """Заявка + недолгое ожидание файла. Возвращает честный ответ в любом случае."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute("INSERT INTO attachment_requests(account_slug, chat_id, msg_id, requested_at,"
               " state, error) VALUES(?,?,?,?, 'pending', NULL)"
               " ON CONFLICT(account_slug, chat_id, msg_id) DO UPDATE SET"
               " requested_at=excluded.requested_at, state='pending', error=NULL",
               (slug, chat, msg_id, now))
    db.commit()
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(1)
        row = db.execute("SELECT path, mime, size FROM attachments WHERE account_slug=?"
                         " AND chat_id=? AND msg_id=?", (slug, chat, msg_id)).fetchone()
        if row and Path(row[0]).exists():
            return {"cached": False, "path": row[0], "mime": row[1], "size": row[2]}
        st = db.execute("SELECT state, error FROM attachment_requests WHERE account_slug=?"
                        " AND chat_id=? AND msg_id=?", (slug, chat, msg_id)).fetchone()
        if st and st[0] == "failed":
            return {"error": st[1] or "live service could not fetch it"}
    return {"queued": True,
            "why": ("the live service holds this account's Telegram key and is fetching it; "
                    "ask for this attachment again in a few seconds"),
            "state": "pending"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--chat", type=int, required=True)
    ap.add_argument("--msg", type=int, required=True)
    ap.add_argument("--db", default=str(HERE / "data" / "tg.db"))
    ap.add_argument("--out", default=str(HERE / "data" / "attachments"))
    ap.add_argument("--sessions", default=str(HERE / "sessions"),
                    help="каталог сессий; у tg_sync и tg_live он настраивается, здесь тоже")
    ap.add_argument("--wait", type=int, default=25,
                    help="сколько секунд ждать заявку, если ключ держит живой сервис")
    a = ap.parse_args()

    out_dir = Path(a.out)
    sess = Path(a.sessions) / f"{a.slug}.string"

    db = sqlite3.connect(a.db)
    db.executescript((HERE / "schema.sql").read_text())

    try:
        lock = AccountLock(a.slug).__enter__()
    except SystemExit:
        # Сессия здесь не нужна и проверять её нельзя: качать будет держатель
        # ключа, а не мы. Требовать её на этом пути значило бы отказывать по
        # причине, которая к делу не относится.
        # Ключ занят живым процессом — и занят он навсегда, а не «пока идёт синк».
        out = _enqueue_and_wait(db, a.slug, a.chat, a.msg, a.wait)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if "error" not in out else 1

    try:
        if not sess.exists():
            print(json.dumps({"error": f"no session for {a.slug}"}))
            return 1
        readonly.install()
        client = TelegramClient(StringSession(sess.read_text().strip()), API_ID, API_HASH,
                                receive_updates=False)
        await client.connect()
        try:
            out = await download(client, db, a.slug, a.chat, a.msg, out_dir)
        finally:
            await client.disconnect()
    finally:
        lock.__exit__()

    print(json.dumps(out, ensure_ascii=False))
    return 0 if "error" not in out else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
