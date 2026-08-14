#!/usr/bin/env python3
"""board — everything in flight, sorted by whether it needs plink.

WHY THIS EXISTS. 2026-08-14: "I'm losing track of all the builds, proposals,
asks, etc." Four separate places hold the answer — the gatehouse's branches,
SPECS/ status lines, the broker's proposal log, and the backlog markdown — and
none of them says which items are *waiting on a human*. Worse, they lie by
omission in opposite directions: the shelf showed seven build branches when six
were already merged and only one needed reading, while Gable's provisioning fix
sat in a proposal seq for two days and appeared on no list at all, costing three
builds their test runs.

So the organising question is not "what exists" but **"what is waiting for
you"**, and everything else sorts underneath it.

RULE, inherited from `brief`: never print a bare identifier. Every row says what
the thing is and where it lives, because an item you have to go look up is an
item that gets deferred.

Usage:
    board.py                 # the board, in the terminal
    board.py --json          # the same data, for anything else to render
    board.py --html FILE     # write the shareable page
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SPECS = REPO / "SPECS"
DB = REPO / "server" / "data" / "disjorn.db"
GATEHOUSE = Path("/var/lib/disjorn-broker/gatehouse")
AUDIT = Path("/var/log/disjorn-broker/audit.jsonl")
CUSTODIAN = 4
# Proposals older than this are assumed absorbed into a spec or the backlog;
# they stay in --json but drop off the board so it stays readable.
PROPOSAL_WINDOW_DAYS = 14

# Statuses that mean the spec's work is finished and it should stop appearing
# as if something is pending.
DONE_STATUSES = {"applied-live", "merged", "superseded", "abandoned"}


def _run(*args: str) -> str:
    cp = subprocess.run(args, capture_output=True, text=True)
    return cp.stdout if cp.returncode == 0 else ""


def _git_bare(repo: Path, *args: str) -> str:
    return _run("sudo", "git", "--git-dir", str(repo), *args)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── sources ─────────────────────────────────────────────────────────────────

def spec_status(path: Path) -> str:
    """First non-comment line under '## Status'. Mirrors the broker's confirm
    gate (brokerd.parse_spec_status) so the board and the gate never disagree
    about what a spec says."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("## status"):
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if not s or s.startswith("<!--"):
                    continue
                return s.strip("`").strip()
            break
    return ""


def spec_confirm_seq(path: Path) -> "int | None":
    m = re.search(r"#custodian seq\*{0,2}:?\s*\**\s*(\d+)",
                  path.read_text(encoding="utf-8", errors="replace"), re.I)
    return int(m.group(1)) if m else None


def collect_specs() -> list:
    out = []
    for f in sorted(SPECS.glob("*.md")):
        if f.stem in {"README", "TEMPLATE"} or f.stem.startswith("PASSDOWN"):
            continue
        status = spec_status(f)
        out.append({
            "slug": f.stem,
            "path": f"SPECS/{f.name}",
            "status": status,
            "status_word": status.split()[0].strip("`,.—-").lower() if status else "",
            "confirm_seq": spec_confirm_seq(f),
        })
    return out


def collect_branches() -> list:
    """Build branches on the shelf, and — the load-bearing bit — whether each is
    already merged. The shelf showed 7 rows on 2026-08-14 when 6 were merged."""
    out = []
    for repo in sorted(GATEHOUSE.glob("*.git")):
        head = (_git_bare(repo, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
                or "main")
        for ref in _git_bare(repo, "for-each-ref", "--format=%(refname:short)",
                             "refs/heads/loop/*").split():
            merged = subprocess.run(
                ["sudo", "git", "--git-dir", str(repo), "merge-base",
                 "--is-ancestor", ref, head],
                capture_output=True).returncode == 0
            stat = _git_bare(repo, "diff", "--shortstat", f"{head}...{ref}").strip()
            out.append({
                "repo": repo.name[:-4],
                "branch": ref,
                "slug": ref[len("loop/"):],
                "tip": _git_bare(repo, "rev-parse", "--short", ref).strip(),
                "merged": merged,
                "shortstat": stat,
                "subject": _git_bare(repo, "log", "-1", "--format=%s", ref).strip(),
            })
    return out


def collect_running_builds() -> list:
    out = []
    for ln in _run("systemctl", "list-units", "disjorn-build-*",
                   "--no-legend", "--plain").splitlines():
        parts = ln.split()
        if parts and parts[0].startswith("disjorn-build-"):
            unit = parts[0]
            out.append({"unit": unit,
                        "slug": unit[len("disjorn-build-"):].removesuffix(".service"),
                        "state": parts[3] if len(parts) > 3 else "?"})
    return out


def collect_proposals(window_days: int = PROPOSAL_WINDOW_DAYS) -> list:
    """Resident asks filed through the broker. These are the ones that vanish:
    a proposal is a message, and a message nobody transcribes is a decision
    nobody made."""
    if not AUDIT.exists():
        return []
    cutoff = _now() - timedelta(days=window_days)
    out = []
    for line in AUDIT.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("verb") != "file-proposal" or not rec.get("allowed"):
            continue
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            continue
        text = str(rec.get("args", {}).get("text", "")).strip()
        title = next((l.strip() for l in text.splitlines() if l.strip()), "(empty)")
        out.append({
            "ts": rec["ts"],
            "date": rec["ts"][:10],
            "recent": ts >= cutoff,
            "resident": rec.get("resident", "?").removeprefix("res-"),
            "title": title[:180],
            "body": text,
        })
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out


def collect_asks_to_plink(limit: int = 40) -> list:
    """Recent #custodian messages that end in a question or name a keyboard-only
    action, from anyone who is not plink. Deliberately crude — it is a prompt to
    look, not a classifier — and it says so on the board."""
    if not DB.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "select seq, author_type, author_id, created_at, content from messages "
        "where channel_id=? and deleted_at is null order by seq desc limit ?",
        (CUSTODIAN, limit)).fetchall()
    names = {1: "Claudette", 2: "Gable", 3: "broker", 5: "keyboard"}
    out = []
    for r in rows:
        if r["author_type"] == "user":
            continue
        body = r["content"] or ""
        low = body.lower()
        if not any(k in low for k in ("plink", "keyboard", "when you're back",
                                      "when you are back", "your key", "needs a human")):
            continue
        first = next((l.strip() for l in body.splitlines() if l.strip()), "")
        out.append({
            "seq": r["seq"],
            "who": names.get(r["author_id"], f"bot {r['author_id']}"),
            "at": r["created_at"][:16].replace("T", " "),
            "line": re.sub(r"[*`_]", "", first)[:200],
        })
    return out


# ── the board ───────────────────────────────────────────────────────────────

def build_board() -> dict:
    specs = collect_specs()
    branches = collect_branches()
    running = collect_running_builds()
    proposals = collect_proposals()
    asks = collect_asks_to_plink()

    by_slug: dict = {}
    for b in branches:
        by_slug.setdefault(b["slug"], []).append(b)

    waiting, in_flight, tidy = [], [], []

    # 1. Builds finished and unmerged -> the only thing that truly blocks.
    for slug, parts in sorted(by_slug.items()):
        unmerged = [p for p in parts if not p["merged"]]
        if unmerged:
            waiting.append({
                "kind": "build",
                "what": f"Build finished and waiting for you to read it: {slug}",
                "where": ", ".join(f"{p['repo']} repo, {p['tip']}" for p in unmerged),
                "detail": "; ".join(p["shortstat"] for p in unmerged if p["shortstat"]),
                "how": f"brief review {slug} --full",
                "slug": slug,
            })
        elif parts:
            tidy.append({
                "kind": "merged-branch",
                "what": f"Already merged, branch still on the shelf: {slug}",
                "where": ", ".join(p["repo"] for p in parts),
                "how": f"sudo git --git-dir {GATEHOUSE}/<repo>.git branch -D loop/{slug}",
                "slug": slug,
            })

    running_slugs = {r["slug"] for r in running}
    built_slugs = set(by_slug)

    for s in specs:
        word = s["status_word"]
        if word in DONE_STATUSES:
            continue
        if s["slug"] in running_slugs:
            in_flight.append({
                "kind": "running",
                "what": f"Build running now: {s['slug']}",
                "where": s["path"], "how": "wait for the reaper's banner",
                "slug": s["slug"]})
        elif word == "confirmed" and s["slug"] not in built_slugs:
            in_flight.append({
                "kind": "ready",
                "what": f"Confirmed and waiting for a resident to build it: {s['slug']}",
                "where": s["path"],
                "how": "a resident presses start_build; nothing needed from you",
                "slug": s["slug"]})
        elif word == "confirmed" and s["slug"] in built_slugs:
            tidy.append({
                "kind": "stale-status",
                "what": f"Spec still says 'confirmed' but its build is merged: {s['slug']}",
                "where": s["path"],
                "how": "set the Status line to 'merged' so it stops looking pending",
                "slug": s["slug"]})
        elif word == "draft":
            waiting.append({
                "kind": "spec",
                "what": f"Spec waiting on your confirm: {s['slug']}",
                "where": s["path"],
                "detail": s["status"][:160],
                "how": "post the nod in #custodian, then fill the Confirm record",
                "slug": s["slug"]})

    return {
        "generated_at": _now().isoformat(timespec="seconds"),
        "waiting": waiting,
        "in_flight": in_flight,
        "tidy": tidy,
        "proposals": [p for p in proposals if p["recent"]],
        "proposals_total": len(proposals),
        "asks": asks,
        "counts": {
            "waiting": len(waiting), "in_flight": len(in_flight),
            "tidy": len(tidy), "specs": len(specs),
            "branches": len(branches),
            "unmerged_branches": sum(1 for b in branches if not b["merged"]),
        },
    }


# ── terminal rendering ──────────────────────────────────────────────────────

def render_text(b: dict, out=sys.stdout) -> None:
    w = out.write
    w("\n")
    n = b["counts"]["waiting"]
    w(f"  THE BOARD — {b['generated_at'][:16].replace('T', ' ')} UTC\n")
    w(f"  {n} thing{'' if n == 1 else 's'} waiting on you.\n\n")

    def section(title: str, items: list, empty: str) -> None:
        w(f"  {title}\n")
        if not items:
            w(f"     {empty}\n\n")
            return
        for it in items:
            w(f"     • {it['what']}\n")
            if it.get("detail"):
                w(f"       {it['detail']}\n")
            w(f"       where: {it['where']}\n")
            w(f"       do:    {it['how']}\n")
        w("\n")

    section("WAITING ON YOU", b["waiting"], "Nothing. Genuinely nothing.")
    section("IN FLIGHT (residents have it)", b["in_flight"], "Nothing running.")

    if b["proposals"]:
        w(f"  RESIDENT PROPOSALS — last {PROPOSAL_WINDOW_DAYS} days "
          f"({len(b['proposals'])} of {b['proposals_total']} ever)\n")
        w("     These are asks filed through the broker. A proposal nobody\n")
        w("     transcribes into SPECS/ or the backlog is a decision nobody made.\n")
        for p in b["proposals"][:12]:
            w(f"     • [{p['date']}] {p['resident']}: {p['title']}\n")
        w("\n")

    if b["asks"]:
        w("  MENTIONS OF YOU IN #custodian (crude keyword match, not a classifier)\n")
        for a in b["asks"][:8]:
            w(f"     • seq {a['seq']} [{a['at']}] {a['who']}: {a['line']}\n")
        w("\n")

    section("TIDYING (safe to ignore; it just makes the board noisy)",
            b["tidy"], "Clean.")


def main(argv=None) -> int:
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit the data, not the view")
    ap.add_argument("--html", metavar="FILE", help="write the shareable page")
    ns = ap.parse_args(argv)

    b = build_board()
    if ns.json:
        print(json.dumps(b, indent=2))
        return 0
    if ns.html:
        from board_html import render_html
        Path(ns.html).write_text(render_html(b), encoding="utf-8")
        print(f"wrote {ns.html}")
        return 0
    render_text(b)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
