#!/usr/bin/env python3
"""Проверяет, что «покажи вложение» работает, когда аккаунт занят живым сервисом.

Ключ авторизации Telegram — ресурс единственного держателя, и живой процесс
держит его ПОСТОЯННО. Значит, второй процесс скачать вложение не может: он
упирается в ту же блокировку. Пока это не было учтено, `tg_attachment` на
развёрнутой системе не работал никогда — и отвечал «аккаунт занят другим
синком», то есть объяснял постоянное состояние временным. Проверить это глазами
нельзя: на ноутбуке, где живого сервиса нет, всё работает.

Три свойства:
  1. блокировка свободна — качаем прямо, ответ содержит путь;
  2. блокировка занята — вместо отказа появляется ЗАЯВКА, и ответ говорит
     «ждите», а не «ошибка»;
  3. заявку выполняет держатель ключа, и файл доходит до просившего.

    python3 test_attachment_queue.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tg_sync import AccountLock                                       # noqa: E402

SLUG = "qtest"
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def build(tmp: Path) -> Path:
    db_path = tmp / "tg.db"
    con = sqlite3.connect(db_path)
    con.executescript((HERE / "schema.sql").read_text())
    con.execute("INSERT INTO accounts(slug, folder_title, added_at, active) VALUES(?,?,?,1)",
                (SLUG, "F", NOW))
    con.execute("INSERT INTO chats(account_slug, chat_id, kind, title, in_scope, first_seen,"
                " last_seen) VALUES(?,?,'group','чат',1,?,?)", (SLUG, 500, NOW, NOW))
    con.commit(); con.close()
    (tmp / "sessions").mkdir()
    # Файл сессии НЕ создаётся: тест не должен ходить в Telegram. На прямом пути
    # это даёт понятный отказ, и проверяется именно то, что заявка при этом не
    # заводится — прямой путь не должен подменяться очередью.
    return db_path


def ask(db_path: Path, wait: int) -> dict:
    """Так его зовёт mcp_server: дочерним процессом, ответ — одна строка JSON."""
    r = subprocess.run([sys.executable, str(HERE / "tg_fetch.py"), "--slug", SLUG,
                        "--chat", "500", "--msg", "7", "--db", str(db_path),
                        "--wait", str(wait), "--sessions", str(db_path.parent / "sessions")],
                       capture_output=True, text=True, timeout=120,
                       cwd=str(HERE))
    line = (r.stdout or r.stderr).strip().splitlines()[-1]
    return json.loads(line)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    db_path = build(tmp)
    ok = True

    # 1. Блокировка свободна: путь прямой. Сессия пустая, поэтому доходим до
    #    Telegram и получаем ошибку — важно, что это ошибка ЧТЕНИЯ, а не отказ
    #    «аккаунт занят», и что заявка при этом не заводится.
    r = ask(db_path, wait=2)
    con = sqlite3.connect(db_path)
    n = con.execute("SELECT COUNT(*) FROM attachment_requests").fetchone()[0]
    print("1) блокировка свободна -> заявок заведено %d (ждём 0), ответ: %r" % (n, r.get("error")))
    ok &= n == 0 and "no session" in (r.get("error") or "")

    # 2. Ключ у живого сервиса. Отказа быть не должно — должна появиться заявка.
    held = AccountLock(SLUG); held.__enter__()
    try:
        r = ask(db_path, wait=2)
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT state FROM attachment_requests WHERE account_slug=?"
                          " AND chat_id=500 AND msg_id=7", (SLUG,)).fetchone()
        print("2) ключ занят -> заявка %s, ответ: %s" % (row and row[0], r.get("queued") and "ждите"
                                                         or r.get("error")))
        ok &= row is not None and row[0] == "pending" and r.get("queued") is True

        # 3. Держатель ключа выполняет заявку — просивший получает файл.
        art = tmp / "art.bin"; art.write_bytes(b"x" * 11)
        con.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?)",
                    (SLUG, 500, 7, str(art), "application/pdf", 11, NOW))
        con.execute("UPDATE attachment_requests SET state='done' WHERE account_slug=?", (SLUG,))
        con.commit()
        r = ask(db_path, wait=5)
        print("3) заявку выполнил держатель ключа -> просивший получил: %s"
              % (Path(r["path"]).name if "path" in r else r))
        ok &= r.get("path") == str(art)
    finally:
        held.__exit__()

    print("\nATTACHMENT QUEUE: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
