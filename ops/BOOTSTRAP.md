# Bootstrap — turn the history you just ingested into the first `brain/`

Run once, right after the first sync. Expect ~30 minutes of agent time.

You are reading a sales team's real Telegram history for the first time. Your job
is **not** to summarise it. It is to turn a stream into a state: the set of files
that make the next question answerable in seconds.

## Order of work

1. **Coverage first.** `tg_coverage`, `tg_accounts`. Write down the date range and
   any failed chats. Everything below is scoped to what actually synced — say so
   explicitly at the top of every file you write.

2. **Cluster the chats before writing anything.** For every in-scope chat decide
   which it is:
   - `counterparty` — a client, prospect, partner or vendor
   - `internal` — colleagues
   - `vendor/support` — a tool's support channel
   - `noise` — announcements, newsletters, dead groups
   Put the mapping in `brain/CHAT_MAP.md` with your reason per chat. Everything
   downstream depends on this split, so it is the one thing worth being slow about.

3. **One file per counterparty** → `brain/accounts/<slug>.md`:
   - who they are, what they want from us, what we want from them
   - who on our side owns it, who on their side decides
   - the state right now, in one sentence, dated
   - **every promise either side made**, each with the message that contains it
   - open questions nobody has answered
   Do not invent structure per company — use `brain/accounts/_TEMPLATE.md`.

4. **One file per recurring person** → `brain/people/<slug>.md`. Only people who
   appear more than once and matter.

5. **The open-asks list** → `brain/tasks/OPEN.md`. Every unfinished ask you found,
   with: what, who asked, when, where (chat + message id), who owns it, and
   whether anything has happened since. Sort by how long it has been sitting.
   This file is usually the first thing that makes somebody say "oh".

6. **Then, and only then, the process draft** → `brain/playbooks/`. You have now
   seen how deals actually move. Write down the shape you observed — the stages,
   what usually stalls them, what a good reply looked like. Say which of it you
   inferred from few examples; a pattern seen twice is a guess, and labelling it
   as one is what makes the rest trustworthy.

## Rules while you do this

- **Cite everything.** A line in an account file with no message behind it is a
  rumour you just promoted to a fact.
- **Do not resolve contradictions silently.** If two chats disagree about what was
  agreed, write both and mark it open. The disagreement is the finding.
- **Do not guess at money.** Amounts, dates and legal terms get copied verbatim
  or left out.
- **Flag what you could not see.** Voice notes without transcripts, attachments
  not fetched, chats that failed to sync. End with a `## What I could not read`
  section. Nothing damages trust in this system faster than a confident summary
  built on 60% of the record.

## When you finish

Print a short report: chats classified (by class), accounts written, open asks
found, and the three things you think are most at risk of being dropped. Then
stop — a human reads it before anything else runs.
