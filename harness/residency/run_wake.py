#!/usr/bin/env python3
"""Entry point for a seat's wake runner (SPECS/2026-08-25-agentic-residents.md).

Usage:
    run_wake.py --config /config/summon.toml [-v]

Same config file as the summon adapter — one seat, one control surface — and it
reads exactly one section of it that the summon adapter ignores: `[wake]`. With
no `[wake].spool_dir` it refuses to start and says so, because a wake runner
that polls nothing is a daemon that looks like a lane.

A SEPARATE PROCESS from run_summon.py, on purpose. The summon adapter serves one
summon at a time inside its event loop; a wake holds a session for up to its
cap, and sharing the loop would mean either a summon waiting an hour behind a
wake or two sessions racing one container name. Two units, two failure domains:
stopping either is a plink-side kill switch for that lane alone.

It opens no websocket. The only thing it sends is the result post, which is an
HTTP call — so this daemon never joins the event stream and never sees a message.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config  # noqa: E402


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Disjorn seat wake runner")
    parser.add_argument(
        "--config",
        default=os.environ.get("SUMMON_CONFIG", "/config/summon.toml"),
        help="path to the seat's TOML config (the summon adapter's file)",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging")
    ns = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config(ns.config)
    if not config.wake.spool_dir:
        print(f"run_wake: no [wake].spool_dir in {ns.config}; this seat has no "
              "wake lane. Configure it (and the broker's [wake] section) or do "
              "not run this daemon.", file=sys.stderr)
        return 2
    api_key = config.resolve_api_key()

    # Deferred imports: keep the module importable (and tests fast) without the
    # SDK's network stack.
    from disjorn_sdk import DisjornClient

    from wake import WakeRunner

    client = DisjornClient(config.server.url, api_key=api_key)
    try:
        runner = WakeRunner(client, config)
    except ValueError as exc:
        # Unsafe config refuses to start, and says which line to fix. Restart=
        # on-failure will retry this forever, which is the intended noise: the
        # lane stays down until a human edits the config.
        print(f"run_wake: refusing to start on {ns.config}: {exc}",
              file=sys.stderr)
        return 2

    async def _run() -> None:
        try:
            await runner.run()
        finally:
            await client.aclose()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
