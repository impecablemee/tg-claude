#!/usr/bin/env python3
"""Проверяет, КАКОЙ чат отвечает на вопрос — без Telegram, на временной базе.

Назвать чат можно тремя способами: id, @username, кусок названия. Последний
удобен людям и опасен: «Клиент Acme» и «Клиент Acme (архив)» оба содержат
запрос. Если инструмент молча берёт первого кандидата, архивный разговор
выдаётся за текущий — и выдаётся КАК ФАКТ, потому что в ответе нет ни следа
того, что выбор вообще был.

Пять свойств:
  1. точное имя побеждает подстроку — назвал точно, получи ответ, а не список;
  2. настоящая двусмысленность отклоняется ВСЕМИ инструментами, берущими чат,
     а не только тем, где это когда-то написали;
  3. поиск с фильтром по чату привязан и к АККАУНТУ: один чат виден двум людям,
     у каждого своя копия строк (правило 1 схемы), и фильтр только по chat_id
     возвращает разговор дважды, причём вторая копия — из области согласия того,
     про кого не спрашивали;
  4. id принимается в том виде, в котором его показывает Telegram (-100…), а не
     только в голом;
  5. несуществующий чат — понятная ошибка, а не пустой ответ.

    python3 test_chat_resolution.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

DB = Path(tempfile.mkdtemp()) / "tg.db"
os.environ["TG_DB"] = str(DB)
sys.path.insert(0, str(HERE))


def build() -> None:
    con = sqlite3.connect(DB)
    con.executescript((HERE / "schema.sql").read_text())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for slug in ("anna", "boris"):
        con.execute("INSERT INTO accounts(slug, folder_title, added_at, active)"
                    " VALUES(?,'F',?,1)", (slug, now))
    chats = [("anna", 100, "Клиент Acme"), ("anna", 200, "Клиент Acme (архив)"),
             ("boris", 100, "Клиент Acme")]          # один чат, две области согласия
    for slug, cid, title in chats:
        con.execute("INSERT INTO chats(account_slug, chat_id, kind, title, in_scope,"
                    " first_seen, last_seen) VALUES(?,?,'group',?,1,?,?)",
                    (slug, cid, title, now, now))
    msgs = [("anna", 100, "живой договор на 100 тысяч"),
            ("anna", 200, "архивный договор на 5 тысяч"),
            ("boris", 100, "договор в копии Бориса")]
    for slug, cid, text in msgs:
        con.execute("INSERT INTO messages(account_slug, chat_id, msg_id, date, out,"
                    " sender_id, text) VALUES(?,?,1,?,0,7,?)", (slug, cid, now, text))
    con.commit()
    con.close()


build()
import mcp_server as M                                                # noqa: E402


def call(tool, **kw):
    fn = getattr(tool, "fn", getattr(tool, "__wrapped__", tool))
    return json.loads(fn(**kw))


def main() -> int:
    ok = True

    r = call(M.tg_history, chat="Клиент Acme", account="anna")
    got = r.get("chat", {}).get("chat_id")
    print("1) точное имя при живом «(архив)» рядом -> чат %s (ждём 100)" % got)
    ok &= got == 100

    print("2) настоящая двусмысленность «Acme»:")
    for name, tool, kw in [("tg_history", M.tg_history, {}),
                           ("tg_thread", M.tg_thread, {"msg_id": 1}),
                           ("tg_participants", M.tg_participants, {}),
                           ("tg_search", M.tg_search, {"query": "договор"})]:
        arg = {"chat": "Acme", "account": "anna", **kw}
        if name == "tg_search":
            arg.pop("account")
            arg["account"] = "anna"
        r = call(tool, **arg)
        good = r.get("error") == "ambiguous"
        print("     %-16s -> %s" % (name, "отказ, кандидаты %d" % len(r.get("candidates", []))
                                    if good else "МОЛЧА ОТВЕТИЛ: %s" % r.get("chat")))
        ok &= good

    # Один и тот же чат есть у обоих: без имени аккаунта это честная
    # двусмысленность, с именем — ровно одна копия, а не две.
    r = call(M.tg_search, query="договор", chat="Клиент Acme")
    print("3) поиск по чату, который есть у двоих, БЕЗ аккаунта -> %s" % r.get("error"))
    ok &= r.get("error") == "ambiguous"

    r = call(M.tg_search, query="договор", chat="Клиент Acme", account="anna")
    accs = sorted({h["account_slug"] for h in r.get("hits", [])})
    texts = [h["snippet"] for h in r.get("hits", [])]
    print("   с аккаунтом anna -> аккаунтов в выдаче: %s, попаданий %d (ждём ['anna'], 1)"
          % (accs, len(texts)))
    ok &= accs == ["anna"] and len(texts) == 1

    r = call(M.tg_history, chat="-100100", account="anna")
    print("4) id как в Telegram (-100100) -> %s" % (r.get("error") or
                                                    "чат %s" % r["chat"]["chat_id"]))
    ok &= r.get("chat", {}).get("chat_id") == 100

    r = call(M.tg_history, chat="ничего такого нет", account="anna")
    print("5) несуществующий чат -> %s" % r.get("error"))
    ok &= "no chat matching" in (r.get("error") or "")

    print("\nCHAT RESOLUTION: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
