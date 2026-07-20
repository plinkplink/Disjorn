"""CLI: `python -m consolidation --resident <name> [--dry-run]`.

Built to be launched on a schedule (broker or systemd timer). Reads the
resident's config, runs the read-only consolidation pass, and either prints the
proposals (`--dry-run`) or posts them to #custodian via the broker file-proposal
CLI.

Safety rails:
  * A resident whose config is not `active` can NEVER post — it is forced to
    dry-run. Claudette ships active (first to activate); Gable ships inactive.
  * Real posting additionally requires the broker CLI to exist. In the build
    worktree it does not, so only `--dry-run` can ever do anything there.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from consolidation.analyze import build_proposals
from consolidation.config import load_config
from consolidation.poster import post_report

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_POST_FAILED = 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m consolidation",
        description="Witnessed memory consolidation (WP-H8) — proposes, never acts.",
    )
    p.add_argument("--resident", required=True, help="resident name (config: <name>.toml)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print proposals to stdout; post nothing (the only mode usable in the worktree)",
    )
    p.add_argument("--config-dir", default=None, help="override config directory")
    p.add_argument("--now", default=None, help="ISO-8601 override for 'now' (testing/backfill)")
    return p


def main(argv: list[str] | None = None, environ=None, out=None) -> int:
    environ = environ if environ is not None else os.environ
    out = out or sys.stdout
    ns = build_parser().parse_args(argv)

    try:
        cfg = load_config(ns.resident, ns.config_dir)
    except (FileNotFoundError, KeyError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    now = None
    if ns.now:
        try:
            now = datetime.fromisoformat(ns.now)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"--now not ISO-8601: {ns.now!r}", file=sys.stderr)
            return EXIT_USAGE

    dry_run = ns.dry_run
    if not dry_run and not cfg.active:
        print(
            f"resident {cfg.resident!r} is not marked active; forcing --dry-run "
            f"(no posting until plink flips it on).",
            file=sys.stderr,
        )
        dry_run = True

    report = build_proposals(cfg, now=now)
    outcome = post_report(report, cfg, dry_run=dry_run, out=out)

    if outcome.dry_run:
        return EXIT_OK
    if not outcome.ok:
        for err in outcome.errors:
            print(f"post error: {err}", file=sys.stderr)
        print(
            f"posted {outcome.posted}, failed {outcome.failed}", file=sys.stderr
        )
        return EXIT_POST_FAILED
    print(f"posted {outcome.posted} proposal(s) to #custodian.", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
