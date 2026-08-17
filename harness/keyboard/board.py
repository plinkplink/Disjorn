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

def _broker():
    """The broker's OWN parsers, imported — never re-implemented here.

    The board's first two days taught why: it read the Status word, saw
    `confirmed`, and reported "nothing waiting on you" while the broker's gate
    was refusing the same spec for a confirm record whose bold was one word
    off. Two parsers of one file will disagree exactly when it matters. So the
    board asks the gate what the gate would say."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "brokerd", REPO / "harness" / "broker" / "brokerd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_BROKER = None


def _broker_load():
    global _BROKER
    if _BROKER is None:
        _BROKER = _broker()
    return _BROKER


def spec_status(path: Path) -> str:
    """First non-comment line under '## Status', via the broker's parser."""
    global _BROKER
    if _BROKER is None:
        _BROKER = _broker()
    return _BROKER.parse_spec_status(
        path.read_text(encoding="utf-8", errors="replace"))


def _spec_status_fallback(path: Path) -> str:
    """Kept only in case brokerd cannot be imported (e.g. a broken checkout)."""
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


def spec_gate(path: Path) -> dict:
    """What the broker's start-build gate would conclude about this spec:
    {'passes': bool, 'why': str}. THIS is what decides whether a 'confirmed'
    spec is genuinely buildable or is silently blocked on a keyboard edit."""
    global _BROKER
    if _BROKER is None:
        _BROKER = _broker()
    text = path.read_text(encoding="utf-8", errors="replace")
    status = _BROKER.parse_spec_status(text)
    rec = _BROKER.parse_confirm_record(text)
    if status != "confirmed":
        return {"passes": False, "why": f"Status is {status!r}, not 'confirmed'",
                "seq": rec.get("seq")}
    if not rec.get("confirmed_by") or not rec.get("seq"):
        return {"passes": False,
                "why": "confirm record does not parse — the broker sees no "
                       "'Confirmed by' / '#custodian seq' (placeholder, blank, "
                       "or a markdown slip)", "seq": rec.get("seq")}
    return {"passes": True, "why": "", "seq": rec["seq"]}


def spec_confirm_seq(path: Path) -> "int | None":
    return spec_gate(path)["seq"]


def _clean_status_prose(status: str, cap: int = 150) -> str:
    """The trailing prose on a Status line, as a readable sentence.

    Specs write things like ``draft` — all three lanes have signed. **Moves to
    `confirmed` when plink's key lands**`, and slicing that raw leaves a
    dangling backtick mid-clause. Drop the leading status token, strip markdown
    emphasis, and cut on a sentence boundary where there is one.
    """
    rest = re.sub(r"^\S+\s*[—–-]*\s*", "", status.strip()).strip()
    rest = re.sub(r"[*`_]", "", rest)
    if not rest:
        return ""
    if len(rest) > cap:
        head = rest[:cap]
        stop = max(head.rfind(". "), head.rfind("; "))
        rest = (head[:stop + 1] if stop > cap // 3 else head.rsplit(" ", 1)[0] + "…")
    return rest.strip()


def collect_specs() -> list:
    out = []
    for f in sorted(SPECS.glob("*.md")):
        if f.stem in {"README", "TEMPLATE"} or f.stem.startswith("PASSDOWN"):
            continue
        status = spec_status(f)
        gate = spec_gate(f)
        out.append({
            "slug": f.stem,
            "path": f"SPECS/{f.name}",
            "status": status,
            "status_word": status.split()[0].strip("`,.—-").lower() if status else "",
            "confirm_seq": gate["seq"],
            "gate_passes": gate["passes"],
            "gate_why": gate["why"],
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


def merged_slugs() -> dict:
    """slug -> merge commit (short) for every spec whose build has landed on
    main. Two witnesses, either suffices:
      * a merge commit on main whose message names the slug (the merge ritual
        writes `merge: <slug> (SPECS/<slug>.md, confirmed seq N)`), or
      * a gatehouse `loop/<slug>` branch that is an ancestor of main.
    The second catches builds merged by hand without the ritual; the first
    catches builds whose branch was already deleted from the shelf."""
    out: dict = {}
    for ln in _run("git", "-C", str(REPO), "log", "--merges", "--format=%h %s",
                   "main").splitlines():
        sha, _, msg = ln.partition(" ")
        for m in re.findall(r"(20\d\d-\d\d-\d\d-[a-z0-9][a-z0-9-]{0,50})", msg):
            out.setdefault(m, sha)
    for repo in sorted(GATEHOUSE.glob("*.git")):
        head = (_git_bare(repo, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
                or "main")
        for ref in _git_bare(repo, "branch", "--merged", head).split():
            if ref.startswith("loop/"):
                out.setdefault(ref[len("loop/"):],
                               _git_bare(repo, "rev-parse", "--short", ref).strip())
    return out


# The words a merged spec may still carry that `--mark-merged` advances. Since
# 2026-08-17 the broker moves `confirmed` -> `building` -> `built@<branch>` /
# `failed` as the build runs (brokerd._stamp_spec_status); the merge is the one
# transition the broker never sees, so it stays the board's to write. `failed`
# is included on purpose: a build that failed and was then merged by hand IS
# merged, and the evidence (a merge commit / an ancestor branch) outranks the
# word.
MERGEABLE_STATUSES = ("confirmed", "building", "failed")


def _advances_to_merged(word: str) -> bool:
    return word in MERGEABLE_STATUSES or word.startswith("built@")


def mark_merged(dry_run: bool = True) -> list:
    """Advance the Status line of every spec whose build is merged from
    `confirmed` / `building` / `built@<branch>` / `failed` to `merged`, and
    say so in the file.

    WHY THE BOARD DOES THIS AND NOT A HUMAN. Nothing in the loop ever moved a
    spec past `confirmed`: the gate reads it, the build runs, the merge lands,
    and the word stays. On 2026-08-17 seven merged specs still said
    `confirmed`, so the "buildable now" list a resident posted had eleven
    items when four were real, and another resident was about to rebuild two
    finished ones. A status the keyboard must remember to set is the "ratified
    default 2" problem again — a value that drifts from the truth by nothing
    more than time passing. The board already computes the truth; it should
    write it. SPECS/ is resident-unwritable by design (BL-D1), so this runs
    with the keyboard's privilege and nowhere else.

    The FILENAME is deliberately untouched: slug == branch == unit == sidecar
    key, regex-validated in three programs. Renaming a done spec would break
    every backward pointer to it (loop/<slug>, seq citations, the gate's path
    resolution). The Status line is the field that exists for this."""
    _broker_load()
    merged = merged_slugs()
    changed = []
    for f in sorted(SPECS.glob("20*.md")):
        slug = f.stem
        if slug not in merged:
            continue
        text = f.read_text(encoding="utf-8")
        word = _BROKER.parse_spec_status(text) or ""
        if not _advances_to_merged(word):
            continue  # already advanced, or never confirmed — leave it
        stamp = _now().date().isoformat()
        # The broker's own line-rewriter: one shape for a Status line, one
        # parser that reads it back.
        new_text = _BROKER.replace_spec_status(
            text, "merged",
            f"advanced from `{word}` by `board --mark-merged` on {stamp}: "
            f"build merged as {merged[slug]}. The word `{word}` on a merged "
            f"spec made it indistinguishable from a buildable one.")
        if new_text is None:
            continue
        changed.append({"slug": slug, "path": f"SPECS/{f.name}",
                        "merge": merged[slug], "from": word})
        if not dry_run:
            f.write_text(new_text, encoding="utf-8")
    return changed


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
    # "built" = merged, by either witness — a spec whose branch was cleaned off
    # the shelf is still built. Shelf-only detection is how seven merged specs
    # went on reading as buildable.
    built_slugs = set(by_slug) | set(merged_slugs())

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
        elif word == "confirmed" and not s["gate_passes"]:
            waiting.append({
                "kind": "gate-blocked",
                "what": f"Says confirmed, but the build gate would REFUSE it: {s['slug']}",
                "where": s["path"],
                "detail": s["gate_why"],
                "how": "fix the Confirm record so 'Confirmed by' and "
                       "'#custodian seq' both parse; `board` re-checks with the "
                       "broker's own parser",
                "slug": s["slug"]})
        elif word == "confirmed" and s["slug"] not in built_slugs:
            in_flight.append({
                "kind": "ready",
                "what": f"Confirmed and waiting for a resident to build it: {s['slug']}",
                "where": s["path"],
                "how": "a resident presses start_build; nothing needed from you",
                "slug": s["slug"]})
        elif _advances_to_merged(word) and s["slug"] in built_slugs:
            tidy.append({
                "kind": "stale-status",
                "what": f"Merged, but the file still says '{word}': {s['slug']}",
                "where": s["path"],
                "how": "board --mark-merged   (advances every such Status line to 'merged')",
                "slug": s["slug"]})
        elif word == "building":
            # The broker stamped it at start-build and no unit is running now:
            # either the broker is between the build ending and its terminal
            # stamp (seconds), or the reaper died before it could write the
            # word (a broker crash mid-build, with no sidecar to adopt).
            waiting.append({
                "kind": "stuck-building",
                "what": f"Says 'building' but no build is running: {s['slug']}",
                "where": s["path"],
                "how": "check #custodian for its banner; if none came, set the "
                       "Status line back to `confirmed` (or `failed`) by hand",
                "slug": s["slug"]})
        elif word.startswith("built@"):
            in_flight.append({
                "kind": "built",
                "what": f"Built and waiting for review/merge: {s['slug']}",
                "where": s["path"],
                "how": f"brief review {s['slug']} --full; `board --mark-merged` "
                       "after the merge",
                "slug": s["slug"]})
        elif word == "failed":
            waiting.append({
                "kind": "failed-build",
                "what": f"Last build FAILED: {s['slug']}",
                "where": s["path"],
                "how": "read the BUILD FAILED banner in #custodian; to allow "
                       "another attempt set the Status line back to `confirmed`",
                "slug": s["slug"]})
        elif word == "draft":
            waiting.append({
                "kind": "spec",
                "what": f"Spec waiting on your confirm: {s['slug']}",
                "where": s["path"],
                # The Status line is a token followed, sometimes, by prose that
                # runs on for a paragraph. Show the human sentence, stripped of
                # markdown, not the raw fragment — a detail line that ends
                # mid-clause on a stray backtick reads as a rendering bug and
                # costs the row its credibility.
                "detail": _clean_status_prose(s["status"]),
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
    ap.add_argument("--mark-merged", action="store_true",
                    help="advance the Status line of every merged spec from "
                         "'confirmed' / 'building' / 'built@<branch>' / 'failed' "
                         "to 'merged' (writes SPECS/; needs sudo)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --mark-merged: show what would change, write nothing")
    ns = ap.parse_args(argv)

    if ns.mark_merged:
        if _BROKER is None:
            _broker_load()
        changed = mark_merged(dry_run=ns.dry_run)
        verb = "would advance" if ns.dry_run else "advanced"
        if not changed:
            print("nothing to do: no merged spec still says 'confirmed' / "
                  "'building' / 'built@…' / 'failed'.")
            return 0
        for c in changed:
            print(f"  {verb} {c['path']}  (merged as {c['merge']})")
        if not ns.dry_run:
            print("Now: commit SPECS/, then refresh the mirror so residents read it.")
        return 0

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
