#!/usr/bin/env python3
"""Connect ONE person's Telegram account, once, with them present.

    python3 tg_login.py --slug anna --folder Work

Prints a QR code in the terminal (and writes `qr.png`). The person opens
Telegram → Settings → Devices → Link Desktop Device and scans it. If they have a
cloud password, the script asks for it here — it is typed by them, never stored.

On success it writes `sessions/<slug>.string` (mode 0600) and the account row.
That string IS their Telegram. Treat it exactly like their password:

  * it lives on the client's own server, in the client's own directory
  * mode 0600, never in git (`.gitignore` covers `sessions/`)
  * revoking it is one tap for them: Telegram → Devices → terminate this session.
    Tell every person that sentence during onboarding. Consent you cannot revoke
    in one tap is not consent.

**This file deliberately does NOT install the read-only barrier.** QR login needs
`auth.ImportLoginToken`, which the barrier correctly refuses. Logging in is a
one-time act by the account's owner on their own device; reading their history
forever is not. `guard_readonly.py` exempts this file by name and nothing else.

The `--folder` is the promise: only chats the person puts in that Telegram folder
are ever read. Nothing here enforces it — `tg_sync.py` does, and the guard tests
it. This just records what was agreed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

HERE = Path(__file__).resolve().parent
# The public desktop api_id/api_hash. Not a secret, and not an authorisation:
# the auth key created below is what matters, and it belongs to the person.
API_ID = int(os.environ.get("TG_API_ID", "2040"))
API_HASH = os.environ.get("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True, help="stable short name, e.g. 'anna'")
    ap.add_argument("--folder", required=True,
                    help="папка Telegram, чьи чаты можно читать; '*' — весь аккаунт "
                         "(границы нет, выбирается осознанно и владельцем аккаунта)")
    ap.add_argument("--db", default=str(HERE / "data" / "tg.db"))
    ap.add_argument("--sessions", default=str(HERE / "sessions"))
    a = ap.parse_args()

    sess_dir = Path(a.sessions)
    sess_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(sess_dir, 0o700)
    out = sess_dir / f"{a.slug}.string"
    if out.exists():
        print(f"{out} already exists — delete it first if you really mean to re-link.")
        return 1

    client = TelegramClient(StringSession(), API_ID, API_HASH, receive_updates=False)
    await client.connect()

    qr = await client.qr_login()
    try:
        import qrcode  # optional; the URL alone is enough in a pinch
        qrcode.make(qr.url).save(sess_dir / "qr.png")
        code = qrcode.QRCode(); code.add_data(qr.url); code.print_ascii(invert=True)
    except ImportError:
        print("(install `qrcode[pil]` for a scannable image)")
    print(f"\nLink URL: {qr.url}\nTelegram → Settings → Devices → Link Desktop Device\n")

    # The QR rotates every ~30s. Recreate until they scan or we give up.
    from telethon.errors import SessionPasswordNeededError
    deadline = asyncio.get_event_loop().time() + 300
    while True:
        try:
            if await qr.wait(25):
                break
        except asyncio.TimeoutError:
            pass
        except SessionPasswordNeededError:
            pw = input("Cloud (2FA) password — typed by the account owner: ")
            await client.sign_in(password=pw)
            break
        if asyncio.get_event_loop().time() > deadline:
            print("timed out — rerun when the person is at their phone")
            await client.disconnect()
            return 1
        await qr.recreate()
        print("QR refreshed; rescan the image at", sess_dir / "qr.png")

    me = await client.get_me()
    string = client.session.save()
    out.write_text(string)
    os.chmod(out, 0o600)
    (sess_dir / "qr.png").unlink(missing_ok=True)

    db_path = Path(a.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript((HERE / "schema.sql").read_text())
    db.execute(
        "INSERT INTO accounts(slug, tg_user_id, username, display_name, folder_title,"
        " consent_at, added_at, active) VALUES(?,?,?,?,?,?,?,1)"
        " ON CONFLICT(slug) DO UPDATE SET tg_user_id=excluded.tg_user_id,"
        " username=excluded.username, folder_title=excluded.folder_title, active=1",
        (a.slug, me.id, me.username, f"{me.first_name or ''} {me.last_name or ''}".strip(),
         a.folder, _now(), _now()))
    db.commit()
    await client.disconnect()

    print(f"\nlinked: {a.slug} = @{me.username} (id {me.id}), folder {a.folder!r}")
    print(f"session → {out} (0600)")
    print("Tell them now: 'you can cut this off any time — Telegram → Devices → "
          "terminate. And only chats you put in the folder are read.'"
          if a.folder != "*" else
          "\n  ВНИМАНИЕ: область '*' — читается ВЕСЬ аккаунт, включая личные чаты."
          "\n  Скажите об этом человеку прямо; папка существует именно затем, чтобы"
          "\n  этого не происходило по умолчанию.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
