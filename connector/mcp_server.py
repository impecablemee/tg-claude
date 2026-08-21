#!/usr/bin/env python3
"""The interface Claude actually talks to: read-only MCP tools over the synced DB.

Registered in `.mcp.json`, so any Claude Code session on this server — a person's
own session, a shared one, or the standing background one — can ask about the
team's Telegram without a human pasting screenshots.

Two rules this file keeps:

* **It never opens a Telegram client.** Every tool reads SQLite. The one
  exception is `tg_attachment`, which shells out to a short-lived child holding
  the account's flock (see `tg_fetch.py`) rather than keeping a session open
  inside a long-running server — a resident client would starve the sync job and
  risk the auth key.

* **A gap is reported, never rendered as emptiness.** Every tool that can return
  nothing also says whether we ever looked. "No messages from Acme" and "Acme's
  chat has never synced" are different answers, and only one of them is safe to
  act on.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# `FastMCP` was renamed `MCPServer` in mcp 2.0 and moved out of `mcp.server.fastmcp`
# (that module no longer exists). Both are supported so a `pip install mcp` today
# and a pinned 1.x tomorrow behave the same. Verified against mcp 2.0.0.
try:                                          # mcp >= 2.0
    from mcp.server import MCPServer as _Server
except ImportError:                           # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

HERE = Path(__file__).resolve().parent
DB = Path(os.environ.get("TG_DB", HERE / "data" / "tg.db"))

mcp = _Server("telegram-readonly")


def _db() -> sqlite3.Connection:
    if not DB.exists():
        raise RuntimeError(f"no database at {DB} — run tg_sync.py first")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)   # read-only handle, belt and braces
    con.row_factory = sqlite3.Row
    # SQLite's built-in case-folding is ASCII-ONLY: lowercasing 'Рин' in SQL
    # leaves it as 'Рин'. So matching a lowercased Python string against a
    # SQL-lowercased Cyrillic title never hits, and the tool answers "no chat
    # matching 'Рин'" — which reads as "that chat does not exist" rather than
    # "I cannot lowercase your alphabet". For a Russian-speaking team that is
    # every second lookup. Measured on real data; fixed here rather than in the
    # schema, so no re-sync is needed.
    con.create_function("pylower", 1, lambda x: x.lower() if isinstance(x, str) else x,
                        deterministic=True)
    return con


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _resolve_chat(con, chat: str, account: str | None) -> list[sqlite3.Row]:
    """Accept a chat id, an @username, or a title fragment. Ambiguity is reported,
    never silently resolved to the first match — the wrong thread reads as fact."""
    args: list = []
    where = "1=1"
    if account:
        where += " AND account_slug = ?"; args.append(account)
    if chat.lstrip("-").isdigit():
        # Telegram показывает id канала как -100xxxxxxxxxx, а группы как -xxxxxxx;
        # в базе лежит ГОЛЫЙ id, который вернул get_entity. Принимать только голый
        # значит отвечать «нет такого чата» на id, скопированный из самого
        # Telegram, — то есть на самый естественный способ его назвать.
        raw = int(chat)
        cands = {raw}
        t = chat.lstrip("-")
        if t.startswith("100") and len(t) > 3:
            cands.add(int(t[3:]))
        cands.add(int(t))
        where += " AND chat_id IN (%s)" % ",".join("?" * len(cands))
        args += sorted(cands)
    else:
        where += " AND (pylower(username) = ? OR pylower(title) LIKE ?)"
        args += [chat.lstrip("@").lower(), f"%{chat.lower()}%"]
    rows = con.execute(f"SELECT * FROM chats WHERE {where} ORDER BY in_scope DESC, title",
                       args).fetchall()
    # ТОЧНОЕ ИМЯ ПОБЕЖДАЕТ ПОДСТРОКУ. Иначе вопрос «последнее сообщение в чате
    # "клиент: acme"» упирается в неоднозначность, как только рядом заводится
    # "клиент: acme (архив)": обе строки содержат запрос, и человек получает список
    # кандидатов вместо ответа — при том, что назвал чат ТОЧНО. Проверено на живой
    # базе: 4 чата, запрос по полному имени возвращал двух кандидатов.
    # Если точных совпадений несколько — это настоящая неоднозначность (два чата с
    # одинаковым названием), и её по-прежнему разрешает человек, а не мы за него.
    if len(rows) > 1:
        want, want_user = chat.lower(), chat.lstrip("@").lower()
        exact = [r for r in rows
                 if (r["title"] or "").lower() == want
                 or (r["username"] or "").lower() == want_user]
        if len(exact) == 1:
            return exact
    return rows


def _one_chat(con, chat: str, account: str | None):
    """Ровно один чат — или объяснение, почему его нельзя назвать.

    Возвращает `(row, None)` либо `(None, json-ошибка)`. Существует затем, чтобы
    правило «двусмысленность разрешает человек» было ОДНИМ, а не повторялось в
    каждом инструменте. Раньше его соблюдал только `tg_history`, а `tg_thread`,
    `tg_participants`, `tg_search` и `tg_attachment` молча брали первого
    кандидата: на паре «Клиент Acme» и «Клиент Acme (архив)» это выдаёт архивный
    разговор за текущий, и выдаёт как факт — заметить неоткуда.
    """
    found = _resolve_chat(con, chat, account)
    if not found:
        return None, json.dumps({"error": f"no chat matching {chat!r}",
                                 "hint": "call tg_chats first"}, ensure_ascii=False)
    if len({(r["account_slug"], r["chat_id"]) for r in found}) > 1:
        return None, json.dumps(
            {"error": "ambiguous",
             "hint": "name the chat exactly, or pass account=… / chat=<id>",
             "candidates": [dict(r) for r in found[:10]]}, ensure_ascii=False, indent=1)
    return found[0], None


@mcp.tool()
def tg_accounts() -> str:
    """Which Telegram accounts are connected, which folder each one exposes, and
    when it last synced. Start here — an account that has never synced explains
    an empty answer better than any search will."""
    con = _db()
    rows = _rows(con.execute("""
        SELECT a.slug, a.username, a.display_name, a.folder_title, a.consent_at,
               a.last_sync_at, a.active,
               COALESCE(k.transcribe,'unknown') AS voice_transcription, k.note AS voice_note,
               (SELECT COUNT(*) FROM chats c WHERE c.account_slug=a.slug AND c.in_scope=1) AS chats_in_scope,
               (SELECT COUNT(*) FROM sync_state s WHERE s.account_slug=a.slug AND s.status='failed') AS chats_failed,
               (SELECT COUNT(*) FROM sync_state s WHERE s.account_slug=a.slug AND s.status='never') AS chats_never
        FROM accounts a
        LEFT JOIN account_capabilities k ON k.account_slug = a.slug
        ORDER BY a.slug"""))
    return json.dumps({"accounts": rows, "note":
                       "chats_never/chats_failed > 0 means part of the picture is missing.",
                       "note_scope":
                       "folder_title '*' means the WHOLE account is read, not one folder.",
                       "note_voice":
                       "voice_transcription: ok = voice notes carry text; unavailable = this "
                       "account cannot transcribe (see voice_note), so a voice message with no "
                       "text means NOT TRANSCRIBED, never 'nothing was said'; unknown = not "
                       "asked yet."},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def tg_chats(account: str | None = None, query: str | None = None, limit: int = 60) -> str:
    """List in-scope chats (optionally filtered by a title/username fragment),
    newest activity first."""
    con = _db()
    where, args = ["c.in_scope = 1"], []
    if account:
        where.append("c.account_slug = ?"); args.append(account)
    if query:
        where.append("(pylower(c.title) LIKE ? OR pylower(c.username) LIKE ?)")
        args += [f"%{query.lower()}%", f"%{query.lower()}%"]
    args.append(limit)
    return json.dumps({"chats": _rows(con.execute(f"""
        SELECT c.account_slug, c.chat_id, c.kind, c.title, c.username,
               t.last_date, t.last_out, t.last_text, s.status AS sync_status
        FROM chats c
        LEFT JOIN v_threads t ON t.account_slug=c.account_slug AND t.chat_id=c.chat_id
        LEFT JOIN sync_state s ON s.account_slug=c.account_slug AND s.chat_id=c.chat_id
        WHERE {' AND '.join(where)}
        ORDER BY t.last_date DESC NULLS LAST LIMIT ?""", args))},
        ensure_ascii=False, indent=1)


@mcp.tool()
def tg_history(chat: str, account: str | None = None, since: str | None = None,
               until: str | None = None, limit: int = 200) -> str:
    """Messages of one chat in chronological order. `chat` may be an id, @username
    or a title fragment; `since`/`until` are ISO dates (YYYY-MM-DD)."""
    con = _db()
    c, err = _one_chat(con, chat, account)
    if err:
        return err
    where, args = ["m.account_slug=?", "m.chat_id=?"], [c["account_slug"], c["chat_id"]]
    if since:
        where.append("m.date >= ?"); args.append(since)
    if until:
        where.append("m.date <= ?"); args.append(until + "T23:59:59")
    args.append(limit)
    msgs = _rows(con.execute(f"""
        SELECT m.msg_id, m.date, m.out, m.sender_id, p.username AS sender_username,
               COALESCE(p.first_name,'') || ' ' || COALESCE(p.last_name,'') AS sender_name,
               m.text, m.media_type, m.file_name, m.duration, m.reply_to,
               (SELECT t.text FROM transcripts t WHERE t.account_slug=m.account_slug
                  AND t.chat_id=m.chat_id AND t.msg_id=m.msg_id LIMIT 1) AS transcript
        FROM messages m LEFT JOIN people p ON p.tg_user_id = m.sender_id
        WHERE {' AND '.join(where)} ORDER BY m.date LIMIT ?""", args))
    st = con.execute("SELECT status, last_run_at, error FROM sync_state WHERE account_slug=? AND chat_id=?",
                     (c["account_slug"], c["chat_id"])).fetchone()
    out = {"chat": dict(c), "sync": dict(st) if st else {"status": "never"}, "messages": msgs}
    # Голосовое без текста — это пробел, и он обязан назвать себя. Иначе читатель
    # видит `[voice]` и достраивает «наверное, ничего важного».
    mute = sum(1 for m in msgs if m["media_type"] == "voice" and not m["transcript"])
    if mute:
        cap = con.execute("SELECT transcribe, note FROM account_capabilities WHERE account_slug=?",
                          (c["account_slug"],)).fetchone()
        state = cap["transcribe"] if cap else "unknown"
        out["voice_without_transcript"] = mute
        out["note_voice"] = {
            "ok": "these are queued — the next sync pass transcribes them; ask again shortly",
            "unavailable": f"this account cannot transcribe voice ({cap['note'] if cap else ''})"
                           " — the content of these messages is UNKNOWN, not empty",
            "unknown": "transcription has not been attempted for this account yet — the content"
                       " of these messages is UNKNOWN, not empty",
        }[state]
    return json.dumps(out, ensure_ascii=False, indent=1)


@mcp.tool()
def tg_search(query: str, account: str | None = None, chat: str | None = None,
              since: str | None = None, limit: int = 50) -> str:
    """Full-text search across every synced message. Use FTS5 syntax:
    `vIBAN`, `"payment rail"`, `mica NEAR/5 licence`, `refund*`.

    INFLECTED LANGUAGES (Russian and friends): FTS matches whole tokens, so
    `оплата` does NOT find «оплату» — different token, no stemmer. If the exact
    query finds nothing this tool retries it as a prefix search and tells you so
    in `retried_as`; report that, because prefix hits are word-beginnings, not the
    word. For a reliable search, query the STEM: `оплат*`, `продаж*`, `догово*`.
    Zero hits after a retry still means "nothing matched THESE tokens" — not
    "this was never discussed"."""
    con = _db()
    where, args = ["messages_fts MATCH ?"], [query]
    if account:
        where.append("m.account_slug = ?"); args.append(account)
    if since:
        where.append("m.date >= ?"); args.append(since)
    if chat:
        c, err = _one_chat(con, chat, account)
        if err:
            return err
        # И по аккаунту тоже. Один и тот же чат виден двум людям, и у каждого своя
        # копия строк (правило 1 схемы). Фильтр только по chat_id возвращает обе:
        # один разговор дважды, причём вторая копия — из области согласия того,
        # про кого не спрашивали.
        where += ["m.chat_id = ?", "m.account_slug = ?"]
        args += [c["chat_id"], c["account_slug"]]
    args.append(limit)
    try:
        hits = _rows(con.execute(f"""
            SELECT m.account_slug, m.chat_id, c.title, c.username, m.msg_id, m.date, m.out,
                   snippet(messages_fts, 0, '«', '»', '…', 24) AS snippet
            FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid
            JOIN chats c ON c.account_slug=m.account_slug AND c.chat_id=m.chat_id
            WHERE {' AND '.join(where)} ORDER BY m.date DESC LIMIT ?""", args))
    except sqlite3.OperationalError as e:
        return json.dumps({"error": f"bad FTS query: {e}",
                           "hint": "quote phrases; escape a bare '-' or ':'"})

    # НОЛЬ ПОПАДАНИЙ ИЗ-ЗА СКЛОНЕНИЯ — ЭТО НЕ «НИЧЕГО НЕ ПИСАЛИ».
    # FTS5 сравнивает токены целиком: запрос `оплата` не находит «оплату», а
    # `продаж` не находит «продажи». Для русскоязычной команды это каждый второй
    # запрос, и пустой ответ читается как факт («про оплату речи не было»), хотя
    # это свойство поиска, а не переписки. Замерено на живой базе: `продаж` — 0
    # попаданий, `продаж*` — все.
    # Поэтому: точный запрос отрабатывает как раньше, но если он не нашёл НИЧЕГО
    # и в нём нет операторов FTS — повторяем префиксным и ГОВОРИМ, что повторили.
    # Досказать «я искал иначе» честнее, чем молча отдать ноль.
    retried = None
    if not hits and not re.search(r'["*:()]|\b(AND|OR|NOT|NEAR)\b', query):
        terms = [t for t in query.split() if t]
        if terms:
            retried = " ".join(t + "*" for t in terms)
            try:
                args2 = [retried] + args[1:]
                hits = _rows(con.execute(f"""
                    SELECT m.account_slug, m.chat_id, c.title, c.username, m.msg_id, m.date, m.out,
                           snippet(messages_fts, 0, '«', '»', '…', 24) AS snippet
                    FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid
                    JOIN chats c ON c.account_slug=m.account_slug AND c.chat_id=m.chat_id
                    WHERE {' AND '.join(where)} ORDER BY m.date DESC LIMIT ?""", args2))
            except sqlite3.OperationalError:
                retried = None                      # осталось как было: ноль по точному

    out = {"query": query, "hits": hits}
    if retried:
        out["retried_as"] = retried
        out["note_retry"] = ("The exact query matched nothing, so this is the PREFIX search "
                             f"{retried!r}. Say so when reporting: the hits are for word "
                             "beginnings, not for the exact word.")
    out["note"] = "search covers SYNCED messages only — check tg_accounts for gaps"
    return json.dumps(out, ensure_ascii=False, indent=1)


@mcp.tool()
def tg_thread(chat: str, msg_id: int, before: int = 15, after: int = 15,
              account: str | None = None) -> str:
    """The conversation around one message — what a search hit actually means."""
    con = _db()
    c, err = _one_chat(con, chat, account)
    if err:
        return err
    rows = _rows(con.execute("""
        SELECT msg_id, date, out, sender_id, text, media_type FROM messages
        WHERE account_slug=? AND chat_id=? AND msg_id BETWEEN ? AND ?
        ORDER BY msg_id""", (c["account_slug"], c["chat_id"], msg_id - before, msg_id + after)))
    return json.dumps({"chat": dict(c), "around": msg_id, "messages": rows},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def tg_participants(chat: str, account: str | None = None) -> str:
    """Who is in a chat, as far as we have seen them speak or been told."""
    con = _db()
    c, err = _one_chat(con, chat, account)
    if err:
        return err
    rows = _rows(con.execute("""
        SELECT DISTINCT p.tg_user_id, p.username, p.first_name, p.last_name, p.is_bot,
               COUNT(m.msg_id) AS messages
        FROM messages m JOIN people p ON p.tg_user_id = m.sender_id
        WHERE m.account_slug=? AND m.chat_id=? GROUP BY p.tg_user_id
        ORDER BY messages DESC""", (c["account_slug"], c["chat_id"])))
    return json.dumps({"chat": dict(c), "people": rows,
                       "note": "derived from who has SPOKEN — silent members are not listed"},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def tg_waiting_on_us(days: int = 2, account: str | None = None, limit: int = 100) -> str:
    """Threads whose last message came from THEM and is older than `days`.
    The one question a sales team asks every morning."""
    con = _db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    where, args = ["last_out = 0", "last_date <= ?"], [cutoff]
    if account:
        where.append("account_slug = ?"); args.append(account)
    args.append(limit)
    return json.dumps({"cutoff": cutoff, "threads": _rows(con.execute(
        f"SELECT * FROM v_unanswered WHERE {' AND '.join(where)} ORDER BY last_date LIMIT ?", args))},
        ensure_ascii=False, indent=1)


@mcp.tool()
def tg_coverage() -> str:
    """How much of the picture we actually have: rows, date range, failures.
    Read this before making a claim with a number in it."""
    con = _db()
    return json.dumps({
        "totals": dict(con.execute(
            "SELECT COUNT(*) AS messages, MIN(date) AS oldest, MAX(date) AS newest,"
            " COUNT(DISTINCT account_slug || ':' || chat_id) AS chats FROM messages").fetchone()),
        "by_account": _rows(con.execute("""
            SELECT account_slug, COUNT(*) AS messages, MIN(date) AS oldest, MAX(date) AS newest
            FROM messages GROUP BY account_slug""")),
        "sync_status": _rows(con.execute(
            "SELECT account_slug, status, COUNT(*) AS chats FROM sync_state"
            " GROUP BY account_slug, status")),
        # backfill_done=0 means we hold the newest N of that chat and there is
        # older history we have not read. Reporting it is the difference between
        # "this chat is short" and "I only looked at the top of it".
        "incomplete_history": _rows(con.execute("""
            SELECT s.account_slug, s.chat_id, c.title, s.oldest_msg_id,
                   (SELECT MIN(date) FROM messages m WHERE m.account_slug=s.account_slug
                      AND m.chat_id=s.chat_id) AS have_since
            FROM sync_state s JOIN chats c ON c.account_slug=s.account_slug AND c.chat_id=s.chat_id
            WHERE s.backfill_done = 0 AND c.in_scope = 1 LIMIT 30""")),
        "failures": _rows(con.execute(
            "SELECT account_slug, chat_id, error, last_run_at FROM sync_state"
            " WHERE status='failed' LIMIT 20")),
        "last_runs": _rows(con.execute(
            "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 5")),
    }, ensure_ascii=False, indent=1)


@mcp.tool()
def tg_refresh(account: str | None = None, limit: int = 60) -> str:
    """Pull new messages RIGHT NOW, before answering. Use it whenever the question
    is about something that just happened — "the last message in X", "what did they
    just send" — because the scheduled sync runs on a timer and the newest message
    may simply not be in the database yet.

    Returns what actually changed, so "nothing new" is distinguishable from
    "could not look". Reading only: this runs the same syncer cron runs."""
    # Почему это отдельный инструмент, а не автоматика внутри каждого чтения:
    # синк ходит в Telegram, а Telegram считает частоту. Дёргать его на каждый
    # вопрос — способ поймать FLOOD_WAIT на ровном месте. Здесь его зовёт тот, кто
    # знает, что спрашивает про только что случившееся.
    here = Path(__file__).resolve().parent
    py = here / ".venv" / "bin" / "python"
    cmd = [str(py if py.exists() else sys.executable), str(here / "tg_sync.py"),
           "--limit", str(max(1, min(limit, 500)))]
    if account:
        cmd += ["--slug", account]
    before = _db().execute("SELECT COUNT(*) AS n, MAX(date) AS newest FROM messages").fetchone()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return json.dumps({"refreshed": False,
                           "why": "sync did not finish in 300s; answer from what is already "
                                  "stored and say it may be stale"}, ensure_ascii=False)
    after = _db().execute("SELECT COUNT(*) AS n, MAX(date) AS newest FROM messages").fetchone()
    tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
    # Занятая блокировка — не ошибка и, что важнее, чаще всего не «идёт синк».
    # Ключ Telegram держит ЖИВОЙ процесс, и держит постоянно: пока он поднят,
    # свежесть и так секундная, а обновлять нечего. Написать здесь «уже идёт
    # плановый синк» значило бы объяснять постоянное состояние временным.
    busy = any("another sync holds" in t for t in tail)
    return json.dumps({
        "refreshed": r.returncode == 0 or busy,
        "already_running": busy,
        "new_messages": (after["n"] or 0) - (before["n"] or 0),
        "newest_before": before["newest"], "newest_after": after["newest"],
        "log": tail,
        "note": ("This account's Telegram key is held by the live service, which writes new "
                 "messages within seconds and reconciles on a timer. Nothing to refresh: the "
                 "data is already current. (If no live service is deployed, a scheduled sync "
                 "is running right now and will finish on its own.)" if busy else
                 "new_messages=0 means the sync ran and found nothing new — that is an "
                 "answer, not a failure."),
    }, ensure_ascii=False, indent=1)


@mcp.tool()
def tg_attachment(chat: str, msg_id: int, account: str | None = None) -> str:
    """Download one attachment and return its local path.

    Two paths, and the caller does not choose: if nothing holds the account, a
    short-lived child fetches it directly; if the live service holds the Telegram
    key — which it does permanently, by design — the request is queued and that
    service fetches it within seconds. A reply with `queued: true` means "ask
    again in a moment", not "failed"."""
    con = _db()
    c, err = _one_chat(con, chat, account)
    if err:
        return err
    have = con.execute("SELECT path, mime, size FROM attachments WHERE account_slug=? AND chat_id=?"
                       " AND msg_id=?", (c["account_slug"], c["chat_id"], msg_id)).fetchone()
    if have and Path(have["path"]).exists():
        return json.dumps({"cached": True, **dict(have)})
    proc = subprocess.run(
        [sys.executable, str(HERE / "tg_fetch.py"), "--slug", c["account_slug"],
         "--chat", str(c["chat_id"]), "--msg", str(msg_id)],
        capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return json.dumps({"error": (proc.stderr or proc.stdout)[-400:]})
    return proc.stdout.strip().splitlines()[-1]


if __name__ == "__main__":
    mcp.run()
