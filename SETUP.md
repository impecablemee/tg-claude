# Setup — in this order

Do not reorder. Each step's failure mode is visible only if the one before it is
already working.

## 0. What you need before starting

- A Linux server you control (2 vCPU / 4 GB is plenty). Everything lives here:
  the database, the sessions, the repo. Nothing leaves it.
- A git repository for this tree. If there isn't one, `git init` here — the repo
  IS the deliverable, and it must be yours, not ours.
- A Claude Code credential for the server (see step 3 — decide this before you start).
- Telegram on a phone for each person joining, and 10 minutes of their attention.

## 1. The repository (already done)

The system needs somewhere to write before it reads anything — and that is this
repository, which you are already reading. Nothing to provision.

Two things to decide now, while it is still cheap:

- **Fork it or take it over.** This tree is meant to become *yours*: `brain/`
  fills up with your accounts, your people, your decisions. Keep it private.
- **Protect `main` and require pull requests.** The agent should propose changes,
  not push to `main`. GitLab: Settings → Repository → Protected branches.

If you are folding this into an existing repo, put it in its own subdirectory —
`connector/`, `brain/` and `.mcp.json` expect to sit together, and paths in
`.mcp.json` are relative to the repo root you run `claude` from.

## 2. Server + code

```bash
sudo apt update && sudo apt install -y python3.12-venv git flock
git clone <your-repo> ~/ops && cd ~/ops
python3 -m venv connector/.venv
connector/.venv/bin/pip install -r connector/requirements.txt
connector/.venv/bin/python connector/guard_readonly.py     # must print OK before anything else
```

That last line is the point of the whole step: you have proof, on your own
machine, that this code cannot send, *before* you connect a single account.

## 3. Claude Code on the server

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude auth login          # opens a link; paste the code back
claude --version
```

**Decide whose credential this is.** The server runs one Claude Code process per
session, under one credential:

- **Recommended — an Anthropic API key or a Claude team/org seat owned by your company.**
  Billing, audit and revocation are yours.

  Simpler still: put Claude Code on each person's own laptop and let it read the
  server's database over ssh — then there is no shared credential to decide about.
  See LOCAL_CLAUDE.md.
- **Not recommended — one person's personal subscription shared by the team.**
  It works technically and it is against Anthropic's terms; it also means one
  person's account carries everyone's usage. Don't build the deployment on it.

Individual employees who have their own Claude subscription keep using it in
their own editor. That is a different thing from the server sessions and the two
do not conflict.

## 4. The Telegram folder — per person, do this BEFORE linking

Each person makes a Telegram folder (name it once, use the same name for
everybody — e.g. `Work`) and puts into it exactly the chats they are willing to
have read. Personal chats stay out. This is the consent boundary and it is theirs
to move at any time.

Tell them, in these words: *"only what's in that folder is read, you can move a
chat out any time, and you can cut the whole thing off in one tap — Telegram →
Settings → Devices → terminate."* Consent you cannot revoke in one tap is not
consent.

## 5. Link the first account

```bash
connector/.venv/bin/python connector/tg_login.py --slug anna --folder Work
```

Prints a QR. The person scans it (Telegram → Settings → Devices → Link Desktop
Device), types their cloud password if they have one, done. The session string
lands in `connector/sessions/anna.string`, mode 0600, git-ignored.

Repeat per person. **One at a time** — see the note in `connector/tg_sync.py`
about auth keys; two processes on one key gets the key revoked, not throttled.

## 6. First sync

```bash
connector/.venv/bin/python connector/tg_sync.py --slug anna
connector/.venv/bin/python -c "
import sqlite3;d=sqlite3.connect('connector/data/tg.db')
print(d.execute('select count(*),min(date),max(date) from messages').fetchone())"
```

Then put it on a timer — every 15 minutes is plenty:

```
*/15 * * * * cd ~/ops && connector/.venv/bin/python connector/tg_sync.py >> data/sync.log 2>&1
```

## 7. Wire Claude to it

`.mcp.json` is already in this repo. Verify from a session in `~/ops`:

```
> use the telegram tools: tg_accounts, then tg_coverage
```

You should see the account, the folder, chat counts, and the date range. If
`chats_never` is not zero, part of the picture is missing — fix that before you
believe any answer.

## 8. The bootstrap run

```
> follow ops/BOOTSTRAP.md
```

This is the one-time clusterisation: it reads what is now in the database and
writes the first version of `brain/` — one file per counterparty, per person,
the open-asks list, and a first pass at how you actually work. Read what it wrote
and correct it. **Its first draft being wrong in places is the point** — arguing
with a written draft takes ten minutes; extracting the same knowledge by
interview takes a week.

## 9. The standing session

```
0 7 * * 1-5 cd ~/ops && claude -p "follow ops/DAILY.md" >> data/daily.log 2>&1
```

This is the difference between a tool you must remember to use and a colleague
who shows up. See `ops/DAILY.md`.

## 10. Pattern mining — the phase that pays for the rest

```
> follow ops/PATTERN_MINING.md
```

Weekly, after two weeks of history has accumulated. Finds the questions the team
asks over and over, writes a tool for each, and files what access it still needs
in `brain/tasks/NEEDS_ACCESS.md`. Everything arrives as a PR.

Read `ops/PATTERN_MINING.md` before the first run — especially the rule that a
pattern seen twice is a guess and gets labelled as one.
