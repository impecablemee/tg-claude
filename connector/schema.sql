-- Telegram read-only connector — the data layer.
--
-- One SQLite file, WAL mode. Deliberately not Postgres for the pilot: it is one
-- server, one writer (the sync job), many readers (Claude sessions), and zero
-- ops. `ops/POSTGRES.md` has the switch when volume or a second server makes it
-- worth it — the schema below is written to port without changes.
--
-- Three rules the schema enforces rather than documents:
--   1. Every row knows WHICH ACCOUNT saw it. Two salespeople in the same group
--      see the same chat; their consent, their folder and their revocation are
--      separate. `account_slug` is in every primary key for that reason.
--   2. Nothing is ever UPDATEd in place except sync bookkeeping. An edited
--      Telegram message lands as a new `edit_date` plus the new text — but the
--      previous text is kept in `message_edits`, because "what did they say
--      before they edited it" is a real question in a sales dispute.
--   3. A gap is visible. `sync_state.status` distinguishes SYNCED from NEVER RUN
--      from FAILED. "No messages" must never be indistinguishable from
--      "we did not look".

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    slug            TEXT PRIMARY KEY,       -- short stable name, e.g. 'anna'
    tg_user_id      INTEGER,
    username        TEXT,
    display_name    TEXT,
    folder_title    TEXT NOT NULL,          -- the ONLY chats we may read
    consent_at      TEXT,                   -- when this person agreed, in writing
    added_at        TEXT NOT NULL,
    last_sync_at    TEXT,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS chats (
    account_slug    TEXT NOT NULL REFERENCES accounts(slug),
    chat_id         INTEGER NOT NULL,
    kind            TEXT NOT NULL,          -- user | group | channel
    -- A StringSession carries NO entity cache, so `get_entity(12345)` fails with
    -- "Could not find the input entity" on a fresh process. Measured, not feared.
    -- Storing the access_hash lets any later process rebuild an InputPeer itself.
    access_hash     INTEGER,
    title           TEXT,
    username        TEXT,
    in_scope        INTEGER NOT NULL DEFAULT 1,  -- still in the folder?
    first_seen      TEXT NOT NULL,
    last_seen       TEXT,
    PRIMARY KEY (account_slug, chat_id)
);

CREATE TABLE IF NOT EXISTS messages (
    account_slug    TEXT NOT NULL,
    chat_id         INTEGER NOT NULL,
    msg_id          INTEGER NOT NULL,
    date            TEXT NOT NULL,          -- ISO-8601 UTC
    out             INTEGER NOT NULL,       -- 1 = our person wrote it
    sender_id       INTEGER,
    text            TEXT,
    media_type      TEXT,                   -- photo | document | voice | ...
    file_name       TEXT,
    duration        INTEGER,                -- seconds, for voice/video
    reply_to        INTEGER,
    fwd_from        INTEGER,
    edit_date       TEXT,
    PRIMARY KEY (account_slug, chat_id, msg_id),
    FOREIGN KEY (account_slug, chat_id) REFERENCES chats(account_slug, chat_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_date   ON messages(date);
CREATE INDEX IF NOT EXISTS ix_messages_sender ON messages(sender_id);
CREATE INDEX IF NOT EXISTS ix_messages_chat_date ON messages(account_slug, chat_id, date);

-- Full-text search. External-content FTS so the text is stored once.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- Rule 2: an edit does not erase what was said before it.
CREATE TABLE IF NOT EXISTS message_edits (
    account_slug TEXT NOT NULL, chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
    seen_at TEXT NOT NULL, old_text TEXT,
    PRIMARY KEY (account_slug, chat_id, msg_id, seen_at)
);

CREATE TABLE IF NOT EXISTS people (
    tg_user_id   INTEGER PRIMARY KEY,
    username     TEXT,
    first_name   TEXT,
    last_name    TEXT,
    phone        TEXT,
    is_bot       INTEGER DEFAULT 0,
    last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS participants (
    account_slug TEXT NOT NULL, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (account_slug, chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS attachments (
    account_slug TEXT NOT NULL, chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
    path TEXT NOT NULL, mime TEXT, size INTEGER, downloaded_at TEXT NOT NULL,
    PRIMARY KEY (account_slug, chat_id, msg_id)
);

-- Заявки на скачивание вложений.
--
-- Ключ авторизации Telegram — ресурс единственного держателя, и живой процесс
-- держит его постоянно. Значит, второй процесс скачать вложение НЕ МОЖЕТ — он
-- упрётся в ту же блокировку. Пока этой таблицы не было, «скачай вложение» на
-- развёрнутой системе не работало никогда: инструмент отвечал «аккаунт занят»,
-- и это выглядело как временная занятость, хотя занят он навсегда.
--
-- Поэтому не второй клиент, а заявка: её кладёт тот, кто просит, а выполняет
-- тот, у кого ключ. Три состояния, не два: pending ≠ done ≠ failed, и `error`
-- объясняет третье.
CREATE TABLE IF NOT EXISTS attachment_requests (
    account_slug TEXT NOT NULL, chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
    error        TEXT,
    PRIMARY KEY (account_slug, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_attreq_pending ON attachment_requests(state, requested_at);

-- Voice/video notes, transcribed once and kept. Which engine produced it is
-- part of the record: they disagree, and a downstream reader must be able to
-- tell a Telegram transcript from a Gemini one.
CREATE TABLE IF NOT EXISTS transcripts (
    account_slug TEXT NOT NULL, chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
    engine TEXT NOT NULL,               -- telegram_premium | gemini | whisper
    text TEXT NOT NULL, made_at TEXT NOT NULL,
    PRIMARY KEY (account_slug, chat_id, msg_id, engine)
);

-- Что аккаунт УМЕЕТ, спрошенное у Telegram, а не предположенное.
--
-- Расшифровка голосовых — премиальная возможность. Если её нет, `transcripts`
-- просто остаётся пустой, и тогда «голосовое без текста» читается как «там
-- ничего не сказали». Это тот же самый пробел, выданный за пустоту, только про
-- возможность: три состояния — unknown (не спрашивали) ≠ ok ≠ unavailable,
-- и `note` объясняет третье словами Telegram, а не нашей догадкой.
CREATE TABLE IF NOT EXISTS account_capabilities (
    account_slug TEXT PRIMARY KEY,
    transcribe   TEXT NOT NULL DEFAULT 'unknown',   -- unknown | ok | unavailable
    note         TEXT,
    checked_at   TEXT
);

-- Rule 3: three states, never two.
CREATE TABLE IF NOT EXISTS sync_state (
    account_slug  TEXT NOT NULL, chat_id INTEGER NOT NULL,
    last_msg_id   INTEGER,       -- newest message we have; forward sync starts here
    oldest_msg_id INTEGER,       -- oldest message we have
    backfill_done INTEGER NOT NULL DEFAULT 0,  -- have we reached the cutoff / chat start?
    last_run_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'never',   -- never | ok | failed
    error         TEXT,
    PRIMARY KEY (account_slug, chat_id)
);
-- backfill_done = 0 means there is a HOLE between oldest_msg_id and the cutoff:
-- a first run bounded by --limit read the newest N and stopped. That is a
-- legitimate state, but it must never look like "this chat is short". tg_coverage
-- reports it, and the next run keeps paging downward until it closes.

-- Every run of the sync job, so "why is this thread empty" is answerable.
-- status: running | ok | failed | flood_wait
--   `flood_wait` is deliberately NOT `failed`. Telegram asked us to wait longer
--   than `--flood-cap`, so the account stopped part-way: what was read is
--   committed, unreached chats keep their watermarks, and the next run resumes
--   there. Filed as a failure it would send someone debugging a connector that
--   works; `error` carries the seconds to wait.
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id TEXT PRIMARY KEY, account_slug TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT,
    chats_seen INTEGER DEFAULT 0, messages_new INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running', error TEXT
);

-- The last message of every in-scope thread, and who spoke it.
CREATE VIEW IF NOT EXISTS v_threads AS
SELECT m.account_slug, m.chat_id, c.title, c.kind, c.username,
       m.msg_id AS last_msg_id, m.date AS last_date, m.out AS last_out,
       substr(COALESCE(m.text, '[' || COALESCE(m.media_type,'no text') || ']'), 1, 200) AS last_text
FROM messages m
JOIN chats c ON c.account_slug = m.account_slug AND c.chat_id = m.chat_id
WHERE c.in_scope = 1
  AND m.msg_id = (SELECT MAX(x.msg_id) FROM messages x
                  WHERE x.account_slug = m.account_slug AND x.chat_id = m.chat_id);

-- The single most useful operational question: who is waiting on us.
CREATE VIEW IF NOT EXISTS v_unanswered AS
SELECT * FROM v_threads WHERE last_out = 0;
