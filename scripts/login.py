#!/usr/bin/env python3
"""One-time, local-only Telegram login that produces a StringSession.

Run this on YOUR OWN computer, not in chat, not in a shared terminal.

    pip install telethon
    python scripts/login.py

It asks for the phone number of the *spare* account, the login code Telegram
sends to that account, and the 2FA password if you set one. It then prints a
session string, which is the account-equivalent of a password.

What to do with it:
  1. Copy it straight into Render → Environment → TELEGRAM_SESSION_STRING.
  2. Close the terminal. Do not paste it into chat, an issue, a commit, or a
     screenshot. Nobody needs to see it to build or deploy this project.

If you believe a session string leaked: Telegram → Settings → Privacy and
Security → Devices → Terminate all other sessions. That invalidates it.
"""

from __future__ import annotations

import argparse
import getpass
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Telegram StringSession for the spare account.")
    parser.add_argument("--api-id", type=int, help="api_id from https://my.telegram.org")
    parser.add_argument("--api-hash", help="api_hash from https://my.telegram.org")
    parser.add_argument(
        "--out",
        help="Optional file to write the session to. Leave unset unless you "
        "understand the risk; the file must never enter the repository.",
    )
    args = parser.parse_args()

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("telethon is not installed here. Run: pip install telethon", file=sys.stderr)
        return 2

    api_id = args.api_id or int(input("API id (from my.telegram.org): ").strip())
    api_hash = args.api_hash or getpass.getpass("API hash (hidden input): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash)
    print("\nA login code will be sent to the spare account on this device only.\n")
    try:
        client.loop.run_until_complete(client.start(phone=lambda: input("Phone number with country code: ")))
    except Exception as exc:  # noqa: BLE001
        print(f"\nLogin failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "If Telegram asked for a 2FA password, re-run and enter it when prompted.\n"
            "If Telegram blocked the attempt, wait and retry from the same IP; do not "
            "script repeated attempts.",
            file=sys.stderr,
        )
        return 1

    me = client.loop.run_until_complete(client.get_me())
    print(f"\nAuthorized as @{getattr(me, 'username', 'unknown')} (id={me.id})")
    print("This is the account the automation will act as. Confirm it is the spare, not your main.")
    session = client.session.as_string()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(session)
        print(f"Wrote session to {args.out} — chmod 600 it and delete it after pasting into Render.")
    else:
        print("\n--- SESSION STRING (paste into Render env, then clear your scrollback) ---\n")
        print(session)
        print("\n--- END ---\n")

    client.loop.run_until_complete(client.disconnect())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
