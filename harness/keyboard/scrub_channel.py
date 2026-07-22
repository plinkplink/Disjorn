#!/usr/bin/env python3
"""Redact safeguard-tripping text out of a Disjorn channel's backfill window.

Incident tool for the `unbrick-resident` skill. A resident re-reads a channel's
backfill on every summon (and long-lived adapters hold a recent-message buffer
seeded from it), so ONE message carrying safeguard-trigger content ("nanny
buzzer") poisons every later read — the model ingests it and errors/dies before
replying. This redacts the offending messages' content IN PLACE (row, seq, and
reply structure stay intact; only the text is replaced), which clears the
poison from what residents read. The FTS index is kept in sync automatically by
the messages_fts_au trigger (AFTER UPDATE OF content).

DESIGN NOTE — this tool NEVER prints message CONTENT in any mode, on purpose:
its output is safe to paste back to an assistant without re-contaminating that
session. It reports only id / seq / author / timestamp / length. The operator
supplies the trigger fragment via --contains (from argv), so the assistant
driving the incident never has to ingest the poison to run it.

Runs host-side as plink (direct SQLite, WAL-safe). Dry-run by default; needs
--apply to write, and backs the DB up first.

Usage:
  # see what WOULD be scrubbed (no writes); pick ONE selector or combine (=AND):
  python3 scrub_channel.py --channel 4 --seq-range 170 185
  python3 scrub_channel.py --channel 4 --contains 'DISTINCTIVE_FRAGMENT'
  # actually redact (backs up the DB first):
  python3 scrub_channel.py --channel 4 --contains 'DISTINCTIVE_FRAGMENT' --apply

ORDER MATTERS for a long-lived adapter bot (e.g. Claudette): STOP her adapter
BEFORE scrubbing, else she re-emits the poison from her in-RAM buffer faster
than you clean it. Stop -> scrub -> restart. Summon bots (e.g. Gable) are
stateless per invocation, so scrub-then-next-summon suffices.
"""
import argparse
import datetime as _dt
import pathlib
import sqlite3
import sys

DEFAULT_DB = "/home/plink/Disjorn/Disjorn/server/data/disjorn.db"


def _stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def select(con, channel, seq_lo, seq_hi, contains):
    where = ["channel_id = ?", "deleted_at IS NULL"]
    params = [channel]
    if seq_lo is not None:
        where.append("seq >= ?"); params.append(seq_lo)
    if seq_hi is not None:
        where.append("seq <= ?"); params.append(seq_hi)
    if contains is not None:
        where.append("content LIKE ?"); params.append(f"%{contains}%")
    q = (f"SELECT id, seq, author_type, author_id, created_at, length(content) AS len "
         f"FROM messages WHERE {' AND '.join(where)} ORDER BY seq")
    return con.execute(q, params).fetchall()


def backup_db(db_path: str) -> str:
    dest = f"{db_path}.bak-scrub-{_stamp()}"
    src = connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)  # online backup API — safe against the live WAL server
        dst.close()
    finally:
        src.close()
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Redact safeguard-tripping messages from a channel.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--channel", type=int, default=4, help="channel_id (#custodian = 4)")
    ap.add_argument("--seq-range", nargs=2, type=int, metavar=("LOW", "HIGH"))
    ap.add_argument("--contains", type=str, help="substring match (from argv; never stored here)")
    ap.add_argument("--apply", action="store_true", help="actually redact (default: dry-run)")
    ns = ap.parse_args(argv)

    if not ns.seq_range and not ns.contains:
        print("refusing to run with no selector — pass --seq-range and/or --contains", file=sys.stderr)
        return 2
    if not pathlib.Path(ns.db).exists():
        print(f"db not found: {ns.db}", file=sys.stderr)
        return 2
    seq_lo, seq_hi = (ns.seq_range if ns.seq_range else (None, None))
    redaction = f"[redacted {_stamp()}: safeguard-test artifact removed to clear resident backfill]"

    con = connect(ns.db)
    try:
        rows = select(con, ns.channel, seq_lo, seq_hi, ns.contains)
        print(f"channel {ns.channel}: {len(rows)} message(s) match "
              f"(seq_range={ns.seq_range}, contains={'set' if ns.contains else 'none'})")
        print(f"{'id':>6} {'seq':>6}  {'author':>10}  {'created_at':>24}  {'len':>6}")
        for r in rows:
            print(f"{r['id']:>6} {r['seq']:>6}  "
                  f"{r['author_type']+':'+str(r['author_id']):>10}  "
                  f"{r['created_at']:>24}  {r['len']:>6}")
        if not rows:
            print("nothing to do.")
            return 0
        if not ns.apply:
            print("\nDRY RUN — no changes written. Re-run with --apply to redact.")
            return 0

        bak = backup_db(ns.db)
        print(f"\nDB backed up to: {bak}")
        ids = [r["id"] for r in rows]
        with con:  # single transaction; FTS trigger keeps search in sync
            con.executemany(
                "UPDATE messages SET content = ?, edited_at = ? WHERE id = ?",
                [(redaction, _iso_now(), i) for i in ids],
            )
        print(f"redacted {len(ids)} message(s). Backfill for channel {ns.channel} is now clean.")
        print("Already-connected clients keep the old text until they refetch; the next read "
              "(summon backfill / adapter re-seed on restart) is clean.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
