#!/usr/bin/env python3
"""Static half of the read-only promise. Exits 1 — loudly — when it is broken.

Run it in CI, in a pre-commit hook, and on the server hourly. It answers three
questions the runtime barrier cannot:

  1. Does every file that opens a Telegram client also install the barrier?
     A new `sync_v2.py` written by a person or an agent is the realistic way this
     system starts sending, and it will not announce itself.
  2. Does any file name a write API at all? Even unreached, that code is a
     lawsuit exhibit for a client whose sales team's private chats we read.
  3. Does the barrier still actually block? It runs `readonly.selftest()`.

Exemption list is one entry long and carries its reason. Adding a second one is
a decision, not a convenience.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ДВА РАЗНЫХ ВОПРОСА — ДВА РАЗНЫХ ОСВОБОЖДЕНИЯ.
#
# «Открывает клиента, не поставив барьер» и «называет пишущий метод» — про разное,
# и снимать их вместе значит освобождать больше, чем нужно. Файл, чья работа —
# перечислить запрещённое поимённо, обязан называть эти методы; но открывать
# клиента в обход барьера ему по-прежнему нельзя.
EXEMPT = {"tg_login.py": "QR device-link needs auth.ImportLoginToken; human-present, one-time",
          "readonly.py": "is the barrier",
          "guard_readonly.py": "is this guard"}

# Освобождены ТОЛЬКО от переписи пишущих имён, не от проверки на барьер.
EXEMPT_TOKENS = {"test_barrier_holes.py":
                 "перечисляет запрещённые методы поимённо — в этом и состоит проверка"}

OPENS_CLIENT = re.compile(r"\bTelegramClient\s*\(")
INSTALLS = re.compile(r"^\s*import\s+readonly\b|^\s*from\s+readonly\s+import", re.M)

# Write APIs, by the name they appear under in Telethon. Substring match on
# purpose: `client.send_message`, `functions.messages.SendMessageRequest` and
# `SendReactionRequest` must all trip it.
WRITE_TOKENS = (
    "send_message", "send_file", "send_read_acknowledge", "forward_messages",
    "edit_message", "delete_messages", "pin_message", "unpin_message",
    "SendMessageRequest", "SendMediaRequest", "SendReactionRequest",
    "ForwardMessagesRequest", "EditMessageRequest", "DeleteMessagesRequest",
    "ReadHistoryRequest", "JoinChannelRequest", "ImportChatInviteRequest",
    "SetTypingRequest", "UpdateProfileRequest",
)


def main() -> int:
    problems: list[str] = []
    for py in sorted(HERE.glob("*.py")):
        src = py.read_text()
        name = py.name
        if name not in EXEMPT:
            if OPENS_CLIENT.search(src) and not INSTALLS.search(src):
                problems.append(
                    f"{name}: opens a TelegramClient but never imports `readonly`. "
                    f"Add `import readonly` + `readonly.install()`, or justify an "
                    f"EXEMPT entry in guard_readonly.py.")
        if name not in EXEMPT and name not in EXEMPT_TOKENS:
            for tok in WRITE_TOKENS:
                for m in re.finditer(re.escape(tok), src):
                    line = src[:m.start()].count("\n") + 1
                    if tok in ("SendMessageRequest", "ReadHistoryRequest", "JoinChannelRequest",
                               "ForwardMessagesRequest") and name == "readonly.py":
                        continue
                    problems.append(f"{name}:{line}: names a Telegram WRITE api ({tok})")

    # The barrier must not merely exist — it must still block.
    r = subprocess.run([sys.executable, str(HERE / "readonly.py")],
                       capture_output=True, text=True)
    if r.returncode != 0 or "OK" not in r.stdout:
        problems.append(f"readonly.selftest FAILED: {(r.stderr or r.stdout).strip()[:300]}")
    else:
        print(r.stdout.strip())

    if problems:
        print("\nREAD-ONLY GUARD: FAILED\n" + "\n".join(f"  - {p}" for p in problems),
              file=sys.stderr)
        return 1
    print(f"READ-ONLY GUARD: OK ({len(list(HERE.glob('*.py')))} files checked, "
          f"{len(EXEMPT)} exempt, {len(EXEMPT_TOKENS)} exempt from the name scan only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
