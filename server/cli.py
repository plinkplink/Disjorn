#!/usr/bin/env python3
"""Disjorn admin CLI (stdlib argparse; run with the server venv from server/).

Usage:
    .venv/bin/python cli.py create-user <username> [--display-name NAME] [--admin] [--password-stdin]
    .venv/bin/python cli.py create-bot <name>
    .venv/bin/python cli.py gen-vapid

Works against the configured DB (config.py / .env); runs migrations first if
needed. `create-user` prompts for a password via getpass unless
`--password-stdin` is given (reads one line from stdin — for scripts).
`create-bot` prints the raw API key exactly once; only its SHA-256 hash is
stored. `gen-vapid` prints VAPID env lines to paste into .env.
"""

import argparse
import asyncio
import base64
import getpass
import secrets
import sqlite3
import sys

from app import db
from app.main import seed_main_feed
from app.routers.auth import hash_api_key, hash_password


def _fail(msg: str, code: int = 1) -> "None":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            _fail("passwords do not match")
    if not password:
        _fail("password must not be empty")
    return password


async def cmd_create_user(args: argparse.Namespace) -> None:
    password = _read_password(args)
    await db.connect()
    await db.run_migrations()
    try:
        cur = await db.execute(
            """INSERT INTO users (username, password_hash, display_name, is_admin)
               VALUES (?, ?, ?, ?)""",
            (
                args.username,
                hash_password(password),
                args.display_name or args.username,
                1 if args.admin else 0,
            ),
        )
    except sqlite3.IntegrityError:
        _fail(f"user '{args.username}' already exists")
    admin_note = " [admin]" if args.admin else ""
    print(f"Created user '{args.username}' (id={cur.lastrowid}){admin_note}")


async def cmd_create_bot(args: argparse.Namespace) -> None:
    await db.connect()
    await db.run_migrations()
    main_feed_id = await seed_main_feed()
    api_key = secrets.token_urlsafe(32)
    try:
        cur = await db.execute(
            "INSERT INTO bots (name, api_key_hash) VALUES (?, ?)",
            (args.name, hash_api_key(api_key)),
        )
    except sqlite3.IntegrityError:
        _fail(f"bot '{args.name}' already exists")
    bot_id = cur.lastrowid
    await db.execute(
        """INSERT OR IGNORE INTO channel_members (channel_id, member_type, member_id)
           VALUES (?, 'bot', ?)""",
        (main_feed_id, bot_id),
    )
    print(f"Created bot '{args.name}' (id={bot_id}), member of main_feed (channel {main_feed_id})")
    print("API key (shown ONCE — store it now):")
    print(f"  {api_key}")


def cmd_gen_vapid(args: argparse.Namespace) -> None:
    """Generate a VAPID keypair (ECDSA P-256) and print .env lines.

    VAPID_PUBLIC_KEY: base64url (no padding) of the 65-byte uncompressed
    X9.62 point — the browser `applicationServerKey` format.
    VAPID_PRIVATE_KEY: base64url (no padding) of the 32-byte raw private
    value — accepted by pywebpush/py_vapid.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    print("# Paste into server/.env:")
    print(f"VAPID_PUBLIC_KEY={b64url(public_raw)}")
    print(f"VAPID_PRIVATE_KEY={b64url(private_raw)}")
    print("VAPID_CLAIMS_EMAIL=mailto:admin@example.com")


async def _run_db_command(coro) -> None:
    try:
        await coro
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cli.py", description="Disjorn admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_user = sub.add_parser("create-user", help="create a user account")
    p_user.add_argument("username")
    p_user.add_argument("--display-name", default=None)
    p_user.add_argument("--admin", action="store_true", help="grant the is_admin bit")
    p_user.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of stdin instead of prompting",
    )

    p_bot = sub.add_parser("create-bot", help="create a bot (prints API key once)")
    p_bot.add_argument("name")

    sub.add_parser("gen-vapid", help="generate a VAPID keypair for Web Push")

    args = parser.parse_args(argv)

    if args.command == "create-user":
        asyncio.run(_run_db_command(cmd_create_user(args)))
    elif args.command == "create-bot":
        asyncio.run(_run_db_command(cmd_create_bot(args)))
    elif args.command == "gen-vapid":
        cmd_gen_vapid(args)


if __name__ == "__main__":
    main()
