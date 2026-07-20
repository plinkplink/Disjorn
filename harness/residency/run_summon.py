#!/usr/bin/env python3
"""Entry point for the Gable summon adapter daemon (WP-H9).

Usage:
    run_summon.py --config /config/summon.toml [-v]

Loads config, resolves the API key from its file, builds a reconnecting
DisjornClient and the container launcher, and runs the SummonAdapter until
interrupted. Everything operational is config; this file bakes in no prod
values.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Make the flat residency modules importable whether launched as a script or
# from elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Disjorn Gable summon adapter")
    parser.add_argument(
        "--config",
        default=os.environ.get("SUMMON_CONFIG", "/config/summon.toml"),
        help="path to the summon adapter TOML config",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ns = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if ns.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config(ns.config)
    api_key = config.resolve_api_key()

    # Deferred imports: keep the module importable (and tests fast) without the
    # SDK's network stack.
    from disjorn_sdk import DisjornClient

    from adapter import SummonAdapter

    client = DisjornClient(config.server.url, api_key=api_key)
    adapter = SummonAdapter(client, config)

    async def _run() -> None:
        try:
            await adapter.run()
        finally:
            await client.aclose()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
