#!/usr/bin/env python3
"""Folder-scoped incremental ingest: Telegram → SQLite. Read-only, by construction.

    python3 tg_sync.py                 # every active account
    python3 tg_sync.py --slug anna     # one
    python3 tg_sync.py --full          # ignore watermarks, re-read history

Design decisions that were paid for, not guessed:

* **One process per account, always.** A Telegram auth key is a single-holder
  resource: two processes on the same key makes Telegram see it on two IPs and
  HARD-REVOKE it — not a throttle, a revocation, recoverable only by having the
  person re-scan a QR. So each account serialises on its own `flock`, and
  accounts run one after another, never fanned out. This cost us a live session
  once; it is the single most important line in this file.

* **`receive_updates=False`.** Telethon's update loop parses whatever TL layer
  the server speaks; when the library is older than the layer it dies mid-request
  with a bare `CancelledError` that reads like a network error and isn't. We do
  not need updates — we poll.

* **Commit per chat.** A crash 400 chats in must cost 1 chat, not 400. The
  watermark moves only after that chat's rows are committed.

* **The folder is the consent boundary.** Scope comes from `messages.GetDialogFilters`
  every run, not from a list we cached. A chat the person drags OUT of the folder
  stops being read on the next run (`chats.in_scope = 0`); what was already read
  stays, and `--forget-out-of-scope` deletes it if they ask.

* **FLOOD_WAIT stops the ACCOUNT, not just the chat.** Telegram rate-limits per
  auth key, so a flood raised on chat 1 is still in force on chat 2. Catching it
  per chat — marking that one `failed` and moving on — makes the next 198 chats
  fail instantly for the same reason, and the run still finishes `status='ok'`.
  A working folder of ~200 chats then reads as "the connector is broken" rather
  than "we were asked to wait 400 seconds".

  So: short waits are slept through (Telethon does it under `flood_sleep_threshold`),
  longer ones retry the SAME chat a bounded number of times, and a wait past
  `--flood-cap` stops the whole account cleanly — the run is marked `flood_wait`,
  untouched chats keep their watermarks, and the next run resumes exactly there.
  Stopping is not failure: the chats already committed stay committed, and the
  reason is named in `sync_runs.error` instead of being spread over 199 rows.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl import functions, types
from telethon.sessions import StringSession

import readonly  # the barrier. Never remove this import — guard_readonly.py fails CI on it.

HERE = Path(__file__).resolve().parent
API_ID = int(os.environ.get("TG_API_ID", "2040"))
API_HASH = os.environ.get("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")
FIRST_SYNC_DAYS = int(os.environ.get("TG_FIRST_SYNC_DAYS", "180"))

# Waits shorter than this Telethon sleeps through on its own, inside the call.
# Named here rather than left to the library default so that changing it is a
# decision someone made, not a version upgrade.
FLOOD_AUTO_SLEEP = int(os.environ.get("TG_FLOOD_AUTO_SLEEP", "60"))
# Longer waits we sleep ourselves and retry the same chat — bounded, because an
# unbounded sleep is indistinguishable from a hang to whoever is watching.
FLOOD_RETRIES = int(os.environ.get("TG_FLOOD_RETRIES", "3"))

log = logging.getLogger("tg_sync")


class FloodStop(Exception):
    """Telegram asked for longer than we are willing to wait.

    Raised to unwind out of the per-chat loop, never caught by it: the limit is
    on the auth key, so the next chat would fail for the same reason. Carries the
    seconds so the run row can say what to wait for instead of "failed".
    """

    def __init__(self, seconds: int, where: str):
        super().__init__(f"FLOOD_WAIT {seconds}s while {where}")
        self.seconds = seconds
        self.where = where


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _peer_id(peer) -> int | None:
    """Идентификатор из peer-объекта папки. Три разных поля под одно и то же:
    какое заполнено — зависит от того, канал это, группа или личка."""
    for attr in ("channel_id", "chat_id", "user_id"):
        v = getattr(peer, attr, None)
        if v is not None:
            return v
    return None


def _kind(entity) -> str:
    if isinstance(entity, types.User):
        return "user"
    if isinstance(entity, types.Chat):
        return "group"
    if isinstance(entity, types.Channel):
        return "group" if entity.megagroup else "channel"
    return "other"


def store_message(db, slug: int, cid: int, m) -> None:
    """One message + its author. An edit keeps the previous text (see schema rule 2)."""
    mt, fname, dur = _media(m)
    prev = db.execute("SELECT text FROM messages WHERE account_slug=? AND chat_id=? AND msg_id=?",
                      (slug, cid, m.id)).fetchone()
    if prev and prev[0] != (m.message or ""):
        db.execute("INSERT OR IGNORE INTO message_edits VALUES(?,?,?,?,?)",
                   (slug, cid, m.id, _now(), prev[0]))
    db.execute(
        "INSERT INTO messages(account_slug, chat_id, msg_id, date, out, sender_id, text,"
        " media_type, file_name, duration, reply_to, fwd_from, edit_date)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(account_slug, chat_id, msg_id) DO UPDATE SET"
        " text=excluded.text, edit_date=excluded.edit_date",
        (slug, cid, m.id, m.date.isoformat(), int(bool(m.out)), getattr(m, "sender_id", None),
         m.message or "", mt, fname, dur,
         getattr(getattr(m, "reply_to", None), "reply_to_msg_id", None),
         1 if getattr(m, "fwd_from", None) else None,
         m.edit_date.isoformat() if m.edit_date else None))
    s_ = getattr(m, "sender", None)
    if s_ is not None and isinstance(s_, types.User):
        db.execute("INSERT INTO people(tg_user_id, username, first_name, last_name, is_bot,"
                   " last_seen) VALUES(?,?,?,?,?,?)"
                   " ON CONFLICT(tg_user_id) DO UPDATE SET username=excluded.username,"
                   " last_seen=excluded.last_seen",
                   (s_.id, s_.username, s_.first_name, s_.last_name, int(bool(s_.bot)), _now()))


def input_peer(kind: str, chat_id: int, access_hash):
    """Rebuild an InputPeer from what we stored — no entity cache required.

    A StringSession keeps no entity cache, so `client.get_entity(<bare id>)`
    raises "Could not find the input entity" in any process that did not itself
    just read the dialog list. Measured on a live account, not anticipated. The
    folder gives us InputPeers on every run, so we persist the access_hash and
    every later process (attachment fetch, ad-hoc script) rebuilds its own."""
    if kind == "channel" or (kind == "group" and access_hash is not None):
        return types.InputPeerChannel(chat_id, access_hash)
    if kind == "user":
        return types.InputPeerUser(chat_id, access_hash)
    return types.InputPeerChat(chat_id)


def _media(msg):
    """(media_type, file_name, duration) — enough to decide whether to fetch it."""
    if not msg.media:
        return None, None, None
    if msg.voice:
        d = next((a.duration for a in msg.voice.attributes if hasattr(a, "duration")), None)
        return "voice", None, d
    if msg.photo:
        return "photo", None, None
    f = getattr(msg, "file", None)
    if f is not None:
        return "document", f.name, getattr(f, "duration", None)
    return type(msg.media).__name__, None, None


class AccountLock:
    """flock per account — see the module docstring. Fails loudly, never waits forever."""

    def __init__(self, slug: str):
        self.path = Path(f"/tmp/tg_connector_{slug}.lock")

    def __enter__(self):
        self.fh = self.path.open("w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            raise SystemExit(f"another sync holds {self.path} — refusing to open a "
                             f"second client on the same Telegram key")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


ALL_CHATS = "*"          # см. folder_peers: область = весь аккаунт


async def folder_peers(client, folder_title: str):
    """Какие чаты этому аккаунту разрешено читать.

    ДВА РЕЖИМА, и выбирает их владелец аккаунта при заведении (`tg_login.py
    --folder`), а не код:

      имя папки   — читаются только чаты, которые человек сам туда положил. Это
                    режим по умолчанию и он же граница согласия: вынес чат —
                    перестали читать;
      "*"         — читается весь аккаунт: все диалоги, где он состоит. Границы
                    нет. Режим существует, потому что он бывает нужен (одному
                    удобнее держать рабочее в папке, другому — нет), но выбирать
                    его должен человек про СВОЙ аккаунт и осознанно.

    Пустой список — ошибка, а не область: «папка пуста» и «папки нет» неотличимы
    для того, кто читает результат, а последствия у них разные.
    """
    if (folder_title or "").strip() == ALL_CHATS:
        # get_dialogs — это messages.GetDialogs, чтение; барьер его пропускает.
        peers = [d.input_entity for d in await client.get_dialogs() if d.input_entity]
        if not peers:
            raise SystemExit(
                "scope is '*' (the whole account) but the dialog list came back empty — "
                "that is a failed read, not an empty account; refusing to treat it as scope.")
        return peers
    res = await client(functions.messages.GetDialogFiltersRequest())
    for f in getattr(res, "filters", res):
        title = getattr(f, "title", None)
        title = getattr(title, "text", title)          # newer layers wrap it
        if title == folder_title:
            return list(getattr(f, "pinned_peers", []) or []) + \
                   list(getattr(f, "include_peers", []) or [])
    raise SystemExit(
        f"folder {folder_title!r} not found on this account. Available: "
        + ", ".join(repr(getattr(getattr(x, 'title', None), 'text', getattr(x, 'title', None)))
                    for x in getattr(res, "filters", res) if getattr(x, "title", None))
        + " — the folder is the consent boundary; syncing everything instead is not a "
          "fallback. If reading the whole account IS what this person wants, say so "
          "explicitly: tg_login.py --folder '*'."
    )


async def transcribe_pending(client, db: sqlite3.Connection, slug: str,
                             limit: int = 20) -> tuple[int, str]:
    """Расшифровать голосовые, у которых расшифровки ещё нет. Возвращает (сколько, статус).

    Зачем это здесь, а не «потом»: человек говорит «иди разберись в чате, там
    голосовуха» — и без расшифровки инструмент отдаёт `[voice]`, то есть отвечает
    «сообщение есть, содержания нет». Для того, кто читает ответ, это неотличимо
    от пустого сообщения.

    Расшифровка — премиальная возможность Telegram, и ответ на вопрос «есть ли
    она» тоже факт: он пишется в `account_capabilities`, чтобы отсутствие текста
    объяснялось строкой в базе, а не молчанием. Ответ приходит не сразу
    (`pending`), поэтому его опрашивают, а не считают пустым с первого раза.
    """
    rows = db.execute(
        "SELECT m.chat_id, m.msg_id, c.kind, c.access_hash FROM messages m"
        " JOIN chats c ON c.account_slug=m.account_slug AND c.chat_id=m.chat_id"
        " WHERE m.account_slug=? AND m.media_type='voice' AND c.in_scope=1"
        "   AND NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.account_slug=m.account_slug"
        "                     AND t.chat_id=m.chat_id AND t.msg_id=m.msg_id)"
        " ORDER BY m.date DESC LIMIT ?", (slug, limit)).fetchall()
    if not rows:
        return 0, "nothing pending"

    done, state, note = 0, "ok", None
    for cid, mid, kind, ah in rows:
        peer = input_peer(kind, cid, ah)
        text, pending = "", True
        try:
            for _ in range(8):
                r = await client(functions.messages.TranscribeAudioRequest(peer=peer, msg_id=mid))
                text, pending = (r.text or ""), bool(getattr(r, "pending", False))
                if text and not pending:
                    break
                await asyncio.sleep(2)
        except FloodWaitError:
            state, note = "ok", "stopped early on FLOOD_WAIT; the rest stay pending"
            break
        except Exception as e:                       # премиума нет, квота, отказ вендора
            state = "unavailable"
            note = f"{type(e).__name__}: {e}"[:200]
            break
        if text:
            db.execute("INSERT OR REPLACE INTO transcripts VALUES(?,?,?,?,?,?)",
                       (slug, cid, mid, "telegram_premium", text, _now()))
            done += 1
    db.execute("INSERT INTO account_capabilities(account_slug, transcribe, note, checked_at)"
               " VALUES(?,?,?,?) ON CONFLICT(account_slug) DO UPDATE SET"
               " transcribe=excluded.transcribe, note=excluded.note,"
               " checked_at=excluded.checked_at", (slug, state, note, _now()))
    db.commit()
    if done or state != "ok":
        log.info("%s: расшифровано %d голосовых, состояние %s%s",
                 slug, done, state, f" ({note})" if note else "")
    return done, state


async def sync_account(db: sqlite3.Connection, slug: str, folder: str, sessions: Path,
                       full: bool, per_chat_limit: int, backfill_pages: int = 0,
                       flood_cap: int = 900) -> None:
    """Opens the session, then hands off. The split exists so the FLOOD_WAIT paths
    are testable: you cannot ask Telegram to rate-limit you on demand, so the only
    way to prove that behaviour is to inject a client that raises it
    (`test_floodwait.py`). Everything below the seam is the real code path."""
    sess = sessions / f"{slug}.string"
    if not sess.exists():
        log.error("%s: no session at %s — run tg_login.py --slug %s", slug, sess, slug)
        return

    readonly.install()
    client = TelegramClient(StringSession(sess.read_text().strip()), API_ID, API_HASH,
                            receive_updates=False)
    # Named, not inherited from whatever the installed Telethon defaults to: this
    # number decides which waits disappear inside the library and which ones reach
    # our retry, and a library upgrade must not move that line silently.
    client.flood_sleep_threshold = FLOOD_AUTO_SLEEP
    await sync_account_with(db, slug, folder, client, full, per_chat_limit,
                            backfill_pages, flood_cap)


async def sync_account_with(db: sqlite3.Connection, slug: str, folder: str, client,
                            full: bool, per_chat_limit: int, backfill_pages: int = 0,
                            flood_cap: int = 900) -> None:
    run_id = str(uuid.uuid4())
    db.execute("INSERT INTO sync_runs(run_id, account_slug, started_at) VALUES(?,?,?)",
               (run_id, slug, _now()))
    db.commit()
    seen = new = 0

    await client.connect()
    if not await client.is_user_authorized():
        db.execute("UPDATE sync_runs SET status='failed', error=?, finished_at=? WHERE run_id=?",
                   ("session revoked — the person must re-link", _now(), run_id))
        db.commit()
        log.error("%s: session revoked (they terminated it, or it expired). Re-run tg_login.py.", slug)
        await client.disconnect()
        return

    try:
        peers = await folder_peers(client, folder)
        wanted: set[int] = set()

        for peer in peers:
            # Идентификатор лежит в самом peer-объекте папки, и он попадает в
            # `wanted` ДО всякого чтения. Иначе чат, который сегодня не удалось
            # разрешить (сеть моргнула, вендор ответил ошибкой), не попадал в
            # список — и метла в конце прогона снимала его с области, то есть
            # ровно так же, как если бы человек вынес его из папки. Согласие
            # нельзя выводить из технической ошибки; ниже это уже написано про
            # FLOOD_WAIT, и здесь верно по той же причине.
            pid = _peer_id(peer)
            if pid is not None:
                wanted.add(pid)
            try:
                entity = await client.get_entity(peer)
            except FloodWaitError as e:
                # NOT "skipped": a flood here is about the account, not this peer.
                # Swallowing it as an unresolvable peer would drop chats from scope
                # and the `in_scope=0` sweep below would then mark them out of the
                # folder — data loss dressed up as consent.
                raise FloodStop(e.seconds, "resolving a folder peer")
            except Exception as e:                      # deleted account, left channel…
                log.warning("%s: cannot resolve a folder peer (%s) — skipped", slug, type(e).__name__)
                continue
            cid = entity.id
            wanted.add(cid)
            seen += 1
            db.execute(
                "INSERT INTO chats(account_slug, chat_id, kind, access_hash, title, username,"
                " in_scope, first_seen, last_seen) VALUES(?,?,?,?,?,?,1,?,?)"
                " ON CONFLICT(account_slug, chat_id) DO UPDATE SET in_scope=1,"
                " access_hash=excluded.access_hash, title=excluded.title,"
                " username=excluded.username, last_seen=excluded.last_seen",
                (slug, cid, _kind(entity), getattr(entity, "access_hash", None),
                 getattr(entity, "title", None) or
                 f"{getattr(entity,'first_name','') or ''} {getattr(entity,'last_name','') or ''}".strip(),
                 getattr(entity, "username", None), _now(), _now()))

            row = db.execute(
                "SELECT last_msg_id, oldest_msg_id, backfill_done FROM sync_state"
                " WHERE account_slug=? AND chat_id=?", (slug, cid)).fetchone()
            last_id   = int(row[0]) if row and row[0] else 0
            oldest_id = int(row[1]) if row and row[1] else 0
            backfilled = bool(row[2]) if row else False
            if full:
                last_id, oldest_id, backfilled = 0, 0, False

            # Two traversals, because they answer different questions — and the
            # obvious single one is silently wrong. MEASURED on a live account
            # (folder of 5 channels, limit 20):
            #   reverse=True + offset_date=<180d ago>  ->  0 messages. Telethon's
            #       offset_date does NOT mean "newer than" under reverse.
            #   reverse=True + min_id=0                ->  the 20 OLDEST messages
            #       (from 2020), which a "skip anything older than the cutoff"
            #       filter then discards in full. That is how a first sync reports
            #       "5 chats in scope, 0 new messages" and looks like it worked.
            # So: FIRST run walks newest-first and stops at the cutoff; every run
            # after walks FORWARD from the watermark, where min_id > 0 anchors it.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=FIRST_SYNC_DAYS)
                      if not full else None)
            # Re-reading a chat after a flood is safe: `store_message` upserts on
            # (account, chat, msg_id), so an attempt that died halfway costs the
            # walk, never a duplicate row. That is what lets the retry below just
            # start the chat over instead of trying to resume mid-page.
            base_top, base_bottom, base_filled = last_id, oldest_id, backfilled
            attempt = 0
            while True:
              top, bottom, n_new = base_top, base_bottom, 0
              backfilled = base_filled
              pages_left = backfill_pages        # per CHAT, not per run

              try:
                # --- pass 1: everything newer than what we have -----------------
                forward = bool(last_id)
                it = (client.iter_messages(entity, min_id=last_id, limit=per_chat_limit,
                                           reverse=True) if forward
                      else client.iter_messages(entity, limit=per_chat_limit))
                n1 = 0
                async for m in it:
                    n1 += 1
                    top = max(top, m.id)      # seen counts, even if not stored —
                                              # otherwise a chat whose newest message
                                              # predates the cutoff is re-walked forever
                    # Порог возраста — про то, как глубоко копать НАЗАД, и только.
                    # Ход ВПЕРЁД идёт от нашей же отметки: всё, что он встречает,
                    # мы ещё не видели, и старость тут не признак ненужности.
                    # Раньше порог обрывал и его: чат, до которого руки не доходили
                    # дольше срока хранения, догонялся по ОДНОМУ сообщению за
                    # прогон, потому что каждый ход упирался в первое же старое.
                    if cutoff and not forward and m.date < cutoff:
                        backfilled = True     # nothing older is wanted
                        break
                    store_message(db, slug, cid, m)
                    bottom = m.id if not bottom else min(bottom, m.id)
                    n_new += 1
                if not last_id and n1 < per_chat_limit:
                    backfilled = True         # exhausted the chat, not the limit

                # --- pass 2: close the hole a bounded first run left behind -----
                while not backfilled and bottom:
                    n2 = 0
                    async for m in client.iter_messages(entity, max_id=bottom,
                                                        limit=per_chat_limit):
                        n2 += 1
                        if cutoff and m.date < cutoff:
                            backfilled = True
                            break
                        store_message(db, slug, cid, m)
                        bottom = min(bottom, m.id)
                        n_new += 1
                    if n2 < per_chat_limit:
                        backfilled = True     # reached the start of the chat
                    if not pages_left:
                        break                 # one page per run unless asked for more
                    pages_left -= 1

                db.execute("INSERT INTO sync_state(account_slug, chat_id, last_msg_id,"
                           " oldest_msg_id, backfill_done, last_run_at, status, error)"
                           " VALUES(?,?,?,?,?,?,'ok',NULL)"
                           " ON CONFLICT(account_slug, chat_id) DO UPDATE SET"
                           " last_msg_id=excluded.last_msg_id,"
                           " oldest_msg_id=excluded.oldest_msg_id,"
                           " backfill_done=excluded.backfill_done,"
                           " last_run_at=excluded.last_run_at, status='ok', error=NULL",
                           (slug, cid, top or None, bottom or None, int(backfilled), _now()))
                db.commit()                      # per chat — a crash costs one chat
                new += n_new
                break                            # chat done; leave the retry loop
              except FloodWaitError as e:
                # Telethon already slept through anything under its threshold, so
                # arriving here means a LONG wait. Two outcomes, both explicit:
                # wait and redo this chat, or stop the account and say for how long.
                if e.seconds > flood_cap or attempt >= FLOOD_RETRIES:
                    raise FloodStop(e.seconds, f"reading chat {cid}")
                attempt += 1
                log.warning("%s chat %s: FLOOD_WAIT %ss — sleeping, then retry %d/%d",
                            slug, cid, e.seconds, attempt, FLOOD_RETRIES)
                await asyncio.sleep(e.seconds + 2)   # +2: the server's clock is not ours
              except Exception as e:
                db.execute("INSERT INTO sync_state(account_slug, chat_id, last_msg_id,"
                           " oldest_msg_id, backfill_done, last_run_at, status, error)"
                           " VALUES(?,?,?,?,?,?,'failed',?)"
                           " ON CONFLICT(account_slug, chat_id) DO UPDATE SET"
                           " last_run_at=excluded.last_run_at, status='failed',"
                           " error=excluded.error",
                           (slug, cid, top or None, bottom or None, int(backfilled), _now(),
                            f"{type(e).__name__}: {e}"[:400]))
                db.commit()
                log.warning("%s chat %s: %s", slug, cid, e)
                break                            # this chat is done for this run

        # Dragged out of the folder ⇒ out of scope from now on.
        # Reached only on a COMPLETE pass over the folder. After a FloodStop we
        # have seen a prefix of the peers, and "not in `wanted`" would then mean
        # "we stopped before reaching it" — marking those chats out of scope reads
        # as the person having removed them. Consent must not be inferred from a
        # rate limit, so the sweep lives here and the flood path skips it.
        db.execute("UPDATE chats SET in_scope=0 WHERE account_slug=? AND chat_id NOT IN (%s)"
                   % (",".join("?" * len(wanted)) or "NULL"), (slug, *wanted))
        db.execute("UPDATE accounts SET last_sync_at=? WHERE slug=?", (_now(), slug))
        db.execute("UPDATE sync_runs SET status='ok', finished_at=?, chats_seen=?, messages_new=?"
                   " WHERE run_id=?", (_now(), seen, new, run_id))
        db.commit()
        log.info("%s: %d chats in scope, %d new messages", slug, seen, new)
        # Голосовые без текста — это «сообщение есть, содержания нет». Догоняем
        # их здесь же: клиент открыт, а отдельный проход стоил бы второго.
        try:
            await transcribe_pending(client, db, slug)
        except Exception as e:                                        # noqa: BLE001
            log.warning("%s: расшифровка не прошла (%s)", slug, type(e).__name__)
    except FloodStop as e:
        # Not a failure: what was committed stays committed, every chat we did not
        # reach keeps its watermark untouched, and the next run continues from
        # exactly here. The reason is written ONCE on the run instead of being
        # smeared across every remaining chat as `status='failed'`.
        db.execute("UPDATE sync_runs SET status='flood_wait', finished_at=?, chats_seen=?,"
                   " messages_new=?, error=? WHERE run_id=?",
                   (_now(), seen, new, str(e), run_id))
        db.execute("UPDATE accounts SET last_sync_at=? WHERE slug=?", (_now(), slug))
        db.commit()
        log.warning("%s: STOPPED on %s. %d chats read, %d new messages kept. "
                    "Re-run after ~%ds — it resumes where it stopped.",
                    slug, e, seen, new, e.seconds)
    finally:
        await client.disconnect()


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--limit", type=int, default=3000, help="max messages per chat per page")
    ap.add_argument("--backfill", type=int, default=0,
                    help="extra backfill pages per chat per run (0 = one page)")
    ap.add_argument("--flood-cap", type=int, default=900,
                    help="stop the account when Telegram asks to wait longer than this "
                         "(seconds); the run resumes where it stopped")
    ap.add_argument("--db", default=str(HERE / "data" / "tg.db"))
    ap.add_argument("--sessions", default=str(HERE / "sessions"))
    a = ap.parse_args()

    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(a.db)
    db.executescript((HERE / "schema.sql").read_text())

    q = "SELECT slug, folder_title FROM accounts WHERE active=1"
    rows = db.execute(q + (" AND slug=?" if a.slug else ""), (a.slug,) if a.slug else ()).fetchall()
    if not rows:
        log.error("no active accounts%s — run tg_login.py first", f" matching {a.slug!r}" if a.slug else "")
        return 1

    for slug, folder in rows:                    # SEQUENTIAL. See the docstring.
        with AccountLock(slug):
            await sync_account(db, slug, folder, Path(a.sessions), a.full, a.limit, a.backfill,
                               a.flood_cap)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
