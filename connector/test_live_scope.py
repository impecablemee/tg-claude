#!/usr/bin/env python3
"""Проверяет решение «читать или не читать» у живого слушателя — без Telegram.

Живой режим держит соединение открытым и записывает всё, что присылает сервер. А
сервер присылает события по ВСЕМ чатам аккаунта, включая личные, которых в папке
нет. Значит, единственное, что отделяет согласованную переписку от чужой, — вот
эта проверка. Ошибка здесь означает не «неудобно», а «прочитали то, чего человек
не разрешал», и заметить её по логам невозможно.

Четыре свойства:
  1. чат из папки — записывается;
  2. чат НЕ из папки — не записывается вовсе;
  3. пустая область (папку ещё не прочитали) — не записывается НИЧЕГО;
     «не знаю состав папки» не имеет права означать «значит, всё можно»;
  4. кривое событие возвращает ошибку, а не роняет процесс: иначе одно сообщение
     выключает живой режим целиком.

    python3 test_live_scope.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tg_live                                                        # noqa: E402


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


def db_with_schema():
    db = sqlite3.connect(":memory:")
    db.executescript((HERE / "schema.sql").read_text())
    db.execute("INSERT INTO accounts(slug, folder_title, active, added_at) VALUES('t','F',1,?)",
               (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    db.commit()
    return db


def n_messages(db):
    return db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def main() -> int:
    ok = True

    # 1. чат в папке
    db = db_with_schema()
    r = tg_live.store_if_in_scope(db, "t", {100}, Msg(1, Peer(channel_id=100)))
    print("1) чат из папки           -> %-14s сообщений в базе: %d" % (r, n_messages(db)))
    ok &= r == "stored" and n_messages(db) == 1

    # 2. чат вне папки — согласия на него не давали
    db = db_with_schema()
    r = tg_live.store_if_in_scope(db, "t", {100}, Msg(1, Peer(user_id=999)))
    print("2) чат НЕ из папки        -> %-14s сообщений в базе: %d" % (r, n_messages(db)))
    ok &= r == "out_of_scope" and n_messages(db) == 0

    # 3. область пуста — состав папки ещё не прочитан
    db = db_with_schema()
    r = tg_live.store_if_in_scope(db, "t", set(), Msg(1, Peer(chat_id=100)))
    print("3) область не прочитана   -> %-14s сообщений в базе: %d" % (r, n_messages(db)))
    ok &= r == "out_of_scope" and n_messages(db) == 0

    # 4. событие без чата и событие, ломающее запись
    db = db_with_schema()
    r1 = tg_live.store_if_in_scope(db, "t", {100}, Msg(1, Peer()))
    broken = Msg(2, Peer(chat_id=100)); broken.id = None       # NOT NULL в схеме
    r2 = tg_live.store_if_in_scope(db, "t", {100}, broken)
    print("4) без chat_id            -> %s" % r1)
    print("   кривое событие         -> %s" % r2[:60])
    ok &= r1 == "no_chat_id" and r2.startswith("error")

    # 5. три вида peer_id — канал, группа, личка
    for attr, val in (("channel_id", 11), ("chat_id", 22), ("user_id", 33)):
        got = tg_live.chat_id_of(Msg(1, Peer(**{attr: val})))
        ok &= got == val
    print("5) канал/группа/личка     -> id извлекается из всех трёх полей")

    print("\n%s" % ("LIVE-SCOPE SELFTEST: OK" if ok else "LIVE-SCOPE SELFTEST: ПРОВАЛ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
