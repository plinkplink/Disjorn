"""Claudette's store <-> a sorted, ==-comparable JSON export, and the drill's round-trip check.

Run with an interpreter that has house_memory's deps (Claudette's host venv).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

COLLECTION = "claudette_memory"


class RefusingEmbedder:
    """Every record travels with its embedding; a call here means one did not."""

    def embed_document(self, text):
        raise RuntimeError("memory_export: a record has no stored embedding")

    embed_query = embed_document


def _store(data_dir: Path):
    from house_memory import MemoryStore
    return MemoryStore(data_dir, COLLECTION, RefusingEmbedder())


def dumps(records: list[dict]) -> bytes:
    return (json.dumps(records, sort_keys=True) + "\n").encode()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ids_sha256(ids) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def export(data_dir: Path, out: Path) -> dict:
    # Opening a missing dir creates an empty store and exports zero records.
    if not (data_dir / "chroma.sqlite3").is_file():
        raise SystemExit(f"memory_export: no chroma.sqlite3 under {data_dir}")
    store = _store(data_dir)
    records = store.export_all()
    if not records:
        raise SystemExit(f"memory_export: {data_dir} exported zero records")
    out.write_bytes(dumps(records))
    return {"count": len(records), "export_sha256": sha256(out),
            "store_count": store.count(), "ids_sha256": ids_sha256(r["id"] for r in records)}


def verify(export_file: Path, count: int, sha: str, scratch: Path,
           store_count: int | None = None, ids_sha: str | None = None) -> list[str]:
    """Returns failures; empty means the export is intact and round-trips losslessly."""
    fails = []
    got_sha = sha256(export_file)
    if got_sha != sha:
        fails.append(f"export sha {got_sha[:12]} != manifest {sha[:12]}")
    records = json.loads(export_file.read_bytes())
    if len(records) != count:
        fails.append(f"export holds {len(records)} records, manifest says {count}")
    if store_count is not None and store_count != count:
        fails.append(f"source store held {store_count} records but {count} were exported")
    if ids_sha is not None and ids_sha256(r["id"] for r in records) != ids_sha:
        fails.append("export ids differ from the source store's ids")
    if scratch.exists():
        shutil.rmtree(scratch)
    store = _store(scratch)
    store.import_all(records)
    if store.count() != count:
        fails.append(f"scratch store count {store.count()} != manifest {count}")
    if store.export_all() != records:
        fails.append("scratch re-export != export file")
    return fails


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--data-dir", type=Path, required=True)
    e.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--export", type=Path, required=True)
    v.add_argument("--count", type=int, required=True)
    v.add_argument("--sha", required=True)
    v.add_argument("--scratch", type=Path, required=True)
    v.add_argument("--store-count", type=int)
    v.add_argument("--ids-sha")
    ns = p.parse_args(argv)

    if ns.cmd == "export":
        print(json.dumps(export(ns.data_dir, ns.out)))
        return 0
    try:
        fails = verify(ns.export, ns.count, ns.sha, ns.scratch, ns.store_count, ns.ids_sha)
    except Exception as exc:
        fails = [f"round-trip raised {type(exc).__name__}: {exc}"]
    for f in fails:
        print(f, file=sys.stderr)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
