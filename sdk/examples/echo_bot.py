#!/usr/bin/env python3
"""Echo bot — minimal end-to-end example for disjorn_sdk.

Connects to a Disjorn server, and whenever a *user* message mentions the bot
(detected via the server-attached ``context`` block — the server only attaches
it to a mentioned bot's copy, so no name parsing is needed), replies with an
echo of the content:

- as a reply (``reply_to=`` the triggering message),
- tagged ``emotion="happy"`` (server resolves it against the bot's chibi pack;
  silently ignored if the bot has none),
- passing through the triggering message's privacy flags (always ``{}`` in
  practice — the server never delivers flagged messages to bots — but it
  demonstrates the ``privacy_flags=`` passthrough for bots that set their own).

Run:
    python echo_bot.py --url http://localhost:8000 --api-key KEY
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from disjorn_sdk import DisjornClient, Event, MessageCreate, Ready

logger = logging.getLogger("echo_bot")


def make_handler(client: DisjornClient):
    async def handle(event: Event) -> None:
        if isinstance(event, Ready):
            logger.info("ready as bot %s (reconnected=%s)", event.bot_id, event.reconnected)
            return
        if not isinstance(event, MessageCreate):
            return
        msg = event.message
        # Never respond to bots (including ourselves — our own echo would
        # mention our name and loop forever) and skip backfilled history.
        if msg.get("author_type") != "user" or event.backfilled:
            return
        # The server attaches `context` only when this bot was mentioned.
        if event.context is None:
            return
        author = (msg.get("author") or {}).get("name", "someone")
        awake = ", ".join(u["name"] for u in event.context.get("awake_users", [])) or "nobody"
        logger.info("mentioned by %s in channel %s (awake: %s)", author, event.channel_id, awake)
        await client.typing(event.channel_id)
        await client.send(
            event.channel_id,
            f"Echo, {author}: {msg.get('content', '')}",
            reply_to=msg["id"],
            emotion="happy",
            # Passthrough: mirror the triggering message's flags onto the reply.
            privacy_flags=msg.get("privacy_flags") or None,
        )

    return handle


async def main() -> None:
    parser = argparse.ArgumentParser(description="Disjorn echo bot")
    parser.add_argument("--url", required=True, help="server base URL, e.g. http://localhost:8000")
    parser.add_argument("--api-key", required=True, help="bot API key (from cli.py create-bot)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    client = DisjornClient(args.url, api_key=args.api_key)
    try:
        await client.run(make_handler(client))
    finally:
        await client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
