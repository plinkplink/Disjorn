#!/usr/bin/env python3
"""planroom — the Plan Room's derivation service. One derivation, three renderers.

THE LOAD-BEARING RULE (SPECS/2026-08-20-plan-room.md, ruled #custodian seqs
1391 item 1 and 1399): **the board owns no authoritative state.** Every card in
this module is a *rendering* of an artifact that already exists — a `SPECS/`
file's Status line, a confirm seq, a gatehouse branch, a backlog row, a push
log line, deploy provenance. Dragging a card never changes reality; changing
reality moves the card. The board cannot go stale relative to the mirror
because it is not a copy of it. The mirror itself CAN lag, so staleness here is
DECLARED, never denied: `face()` carries the derivation time and the mirror
head it derived from, and every renderer shows both.

WHY DERIVED, ON THE RECORD. The night these specs were reviewed, four
hand-written status lines outlived their truth in a few hours — a memory index,
a memory corpus, the deploy docs, and BUILD-LOOP.md's own lanes section (seqs
1405/1414/1419/1428). Any layer written by hand and read as fact lies
eventually. Cards derive from artifacts so the board has a rebuild path instead
of a memory.

WHAT THE BOARD OWNS NATIVELY, AND ONLY THIS: comments, card order, the blocked
flag + reason, archived. None of it is in this module and none of it is in the
index this module writes. It is server-owned, authoritative, and survives every
rebuild — see `server/app/routers/planroom.py` and migration 009.

THE INDEX IS A CACHE, NEVER A SOURCE. `build_index()` writes a SQLite file that
is rebuilt from the artifacts from zero on every run. Git wins every
disagreement. If the index and the repo disagree, the index is wrong, and the
fix is to rebuild it — there is no path in this house by which the index
teaches git anything. Delete the file and the next rebuild restores it whole.
The server reads this file and nothing else; it never derives.

ONE PARSER FOR THE GATE'S OWN FIELDS. Status and confirm-record parsing come
from `brokerd`'s parsers, always, including in the index builder (seq 1428 P3).
`harness/keyboard/board.py`'s docstring carries the incident verbatim, and it
is carried again here because this module is the one that will be copied next:
the board's first two days it read the Status word itself, saw `confirmed`, and
reported "nothing waiting on you" while the broker's gate was refusing the same
spec for a confirm record whose bold was one word off. Two parsers of one file
will disagree exactly when it matters. So this module asks the gate what the
gate would say, and never re-implements the answer.

WHERE IT RUNS. Broker-side, and only broker-side (seq 1428 P2). Derivation
needs gatehouse access (`sudo git --git-dir`) and `brokerd` imports; the Disjorn
server process has neither and must not grow them. If the server's router ever
grows its own reader of SPECS/ or the gatehouse, that is the forked truth this
module exists to prevent — a defect, not a shortcut.

Usage:
    planroom.py --config /etc/disjorn-broker/broker.toml            # the board
    planroom.py --config ... --json                                 # the data
    planroom.py --config ... --rebuild                              # write index
    planroom.py --config ... --rebuild --index /path/to/index.db
    planroom.py --config ... --transitions                          # rebuild and
                                                                    # print the
                                                                    # #custodian
                                                                    # lines
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
REPO = HARNESS.parent

# Ruled at seq 1391 item 1. Order is the board's left-to-right order and is
# load-bearing in every renderer; do not sort these alphabetically.
COLUMNS = ["Backlog", "Proposed", "Ready", "Building", "Review", "Merged",
           "Archived"]

# One line each, shown as the column's subtitle. These say what a card in the
# column MEANS, which is the difference between a board and a list of words.
COLUMN_BLURBS = {
    "Backlog": "Filed, not yet specced. The only column whose cards may lack a "
               "spec file; a card leaves when a spec is drafted.",
    "Proposed": "A spec exists and says `draft`. Waiting on plink's confirm.",
    "Ready": "`confirmed` — the gate's own launch criterion. A Ready card is "
             "literally pressable; nothing else is.",
    "Building": "`building` now, or `failed` and waiting for a human to reset "
                "it to `confirmed` or abandon it.",
    "Review": "Built and waiting to be read — plus keyboard merges whose review "
              "is still owed, and uncited commits on `main`. This column is the "
              "drift report wearing a UI; its resting state is EMPTY.",
    "Merged": "Landed on `main`. The deploy badge says whether production "
              "actually runs it.",
    "Archived": "Everything that is done. Rendered as a table, not as cards.",
}

DERIVED_ARCHIVE_WORDS = {"superseded", "abandoned"}
MERGED_WORDS = {"merged", "applied-live"}

INDEX_SCHEMA_VERSION = 1

# Slug prefixes for the two card kinds that are not a SPECS/ file. A spec card's
# slug IS its spec filename stem (P5: slug = branch = unit = sidecar key), so
# these prefixes must never be able to collide with one: _SPEC_STEM_RE in
# brokerd requires a leading YYYY-MM-DD, and neither prefix can produce one.
BACKLOG_PREFIX = "backlog-"
KEYBOARD_PREFIX = "keyboard-"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── the broker's own parsers ────────────────────────────────────────────────

_BROKER = None


def brokerd():
    """The broker's OWN parsers, imported — never re-implemented here (P3).

    Reuses an already-imported `brokerd` when there is one: this module is
    imported BY the broker for the board verbs, and loading a second copy of a
    158 KiB daemon module to ask it what a Status line says would be two
    parsers again by another route."""
    global _BROKER
    if _BROKER is not None:
        return _BROKER
    mod = sys.modules.get("brokerd")
    if mod is not None and hasattr(mod, "parse_spec_status"):
        _BROKER = mod
        return _BROKER
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "brokerd", HARNESS / "broker" / "brokerd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules.setdefault("brokerd", mod)
    _BROKER = mod
    return _BROKER


_BOARD = None


def board():
    """`harness/keyboard/board.py` — the aggregation this service generalizes.

    Imported rather than copied, on purpose. board.py already reads SPECS
    Status lines, gatehouse branches, the proposal log and whether a slug is
    merged; the Plan Room is a second RENDERER of that aggregation, not a
    second aggregation. The moment these two disagree about whether a spec is
    merged, one of them is lying to a human about whether work is done."""
    global _BOARD
    if _BOARD is not None:
        return _BOARD
    import importlib.util
    path = HARNESS / "keyboard" / "board.py"
    spec = importlib.util.spec_from_file_location("keyboard_board", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        if sys.path and sys.path[0] == str(path.parent):
            sys.path.pop(0)
    _BOARD = mod
    return _BOARD


_METRICS = None


def metrics():
    """`harness/metrics/metrics.py` — Phase 0's gate detector.

    The tri-state deploy badge and the digest's deploy-drift line are ONE
    computation (seq 1428 P6): Phase 0 shipped `deploy_state()` as a named
    function precisely so this module could call it instead of re-implementing
    it. `gate_drift()` is the same argument for the Review column's auto-cards —
    uncited `main` commits are the digest's question, asked once."""
    global _METRICS
    if _METRICS is not None:
        return _METRICS
    import importlib.util
    path = HARNESS / "metrics" / "metrics.py"
    spec = importlib.util.spec_from_file_location("planroom_metrics", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _METRICS = mod
    return _METRICS


# ── spec field parsing (the fields the gate does NOT own) ───────────────────
#
# Status and the confirm record come from brokerd and only from brokerd (P3).
# Title / tier / lane / review owner / builder are card furniture the gate has
# no opinion about, so they are parsed here — from TEMPLATE.md's headings, which
# is the shape every spec in the house is written to.

_PLACEHOLDER_RE = re.compile(r"^<.*>$", re.DOTALL)
_TIER_RE = re.compile(r"\btier\s*([0-9])\b", re.IGNORECASE)


def _demarkdown(s: str) -> str:
    return re.sub(r"[*`_]", "", s).strip()


def _section(text: str, heading_prefix: str) -> list[str]:
    """The lines under the first `## <heading_prefix>…` heading, up to the next
    `## ` heading. Prefix match, because the house's headings carry a trailing
    parenthetical that is part of the ritual and changes."""
    lines = text.splitlines()
    low = heading_prefix.lower()
    out: list[str] = []
    inside = False
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith("## "):
            if inside:
                break
            inside = s[3:].lstrip().lower().startswith(low)
            continue
        if inside:
            out.append(ln)
    return out


def _bullet(lines: list[str], label: str) -> Optional[str]:
    """`- **Label**: value` -> value, cleaned. None if absent or a placeholder."""
    want = label.lower()
    for ln in lines:
        s = ln.strip()
        if not s.startswith("-"):
            continue
        body = s.lstrip("-").strip()
        m = re.match(r"\*\*(.+?)\*\*\s*:?\s*(.*)$", body, re.DOTALL)
        if not m or m.group(1).strip().lower().rstrip(":") != want:
            continue
        value = m.group(2).strip()
        if not value or _PLACEHOLDER_RE.match(value):
            return None
        return _demarkdown(value)
    return None


def _first_prose(lines: list[str]) -> Optional[str]:
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("<!--"):
            continue
        return _demarkdown(s)
    return None


def spec_title(text: str, slug: str) -> str:
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("# "):
            t = _demarkdown(s[2:])
            t = re.sub(r"^spec\s*:\s*", "", t, flags=re.IGNORECASE).strip()
            return t or slug
    return slug


def parse_spec_fields(text: str, slug: str) -> dict:
    """Card furniture from a spec file. Never the Status, never the confirm
    record — those two are brokerd's, always (P3)."""
    lane_lines = _section(text, "lane")
    tier_line = _first_prose(_section(text, "expected diff tier")) or ""
    tier_m = _TIER_RE.search(tier_line)
    split = _section(text, "cross-lane split")
    applies = (_bullet(split, "applies") or "").lower()
    return {
        "title": spec_title(text, slug),
        "tier": f"Tier {tier_m.group(1)}" if tier_m else None,
        "tier_note": tier_line or None,
        "lane": _bullet(lane_lines, "lane"),
        "review_owner": _bullet(lane_lines, "review owner"),
        "builder": _bullet(_section(text, "builder"), "builder"),
        "cross_lane": applies.startswith("yes"),
        "requester": _bullet(_section(text, "request"), "requester"),
    }


# ── artifact readers ────────────────────────────────────────────────────────

def _sqlite_ro(path: Optional[str]):
    if not path or not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    db.row_factory = sqlite3.Row
    return db


def collect_backlog(message_db: Optional[str], limit: int = 200) -> list:
    """Open rows from the `backlog` table — the one card kind with no spec file.

    `/backlog <text>` is the house's intake half (Architecture §13). A row that
    nobody transcribes into SPECS/ is a request nobody answered, which is
    exactly what a Backlog column is for."""
    db = _sqlite_ro(message_db)
    if db is None:
        return []
    try:
        rows = db.execute(
            "select id, text, author, created_at, status, spec_ref from backlog "
            "where status = 'open' order by id desc limit ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    out = []
    for r in rows:
        text = (r["text"] or "").strip()
        title = next((l.strip() for l in text.splitlines() if l.strip()), "(empty)")
        out.append({"id": r["id"], "title": title[:180], "body": text,
                    "author": r["author"], "created_at": r["created_at"],
                    "spec_ref": r["spec_ref"]})
    return out


def spec_dates(repo: Path, specs_rel: str = "SPECS") -> dict:
    """slug -> {"opened": iso, "updated": iso} from git, in ONE log pass.

    Opened/updated are card fields, and a card field written by hand is the
    thing this whole build exists to not do. Two-and-a-half thousand
    subprocesses (one per spec per field) would also make the timer rebuild
    the most expensive thing the broker does."""
    b = board()
    out: dict = {}
    raw = b._run("git", "-C", str(repo), "log", "--date=iso-strict",
                 "--format=%x00%ad", "--name-only", "--", specs_rel)
    date = None
    for line in raw.splitlines():
        if line.startswith("\x00"):
            date = line[1:].strip()
            continue
        name = line.strip()
        if not name or date is None:
            continue
        stem = Path(name).stem
        rec = out.setdefault(stem, {"opened": date, "updated": date})
        # git log walks newest-first: the first sighting is the latest touch,
        # the last sighting is the introduction.
        rec["opened"] = date
    return out


# ── derivation ──────────────────────────────────────────────────────────────

def _status_word(status: Optional[str]) -> str:
    if not status:
        return ""
    return status.split()[0].strip("`,.—-").lower()


def column_for_status(word: str) -> Optional[str]:
    """The one place a Status word becomes a column. Ruled seq 1391 item 1."""
    if word == "draft":
        return "Proposed"
    if word == "confirmed":
        return "Ready"
    if word in ("building", "failed"):
        return "Building"
    if word.startswith("built@"):
        return "Review"
    if word in MERGED_WORDS:
        return "Merged"
    if word in DERIVED_ARCHIVE_WORDS:
        # A superseded or abandoned spec is DONE, and done is what Archived
        # means ("everything that's done"). It is not Merged: no merge
        # happened, and a column that claims one would be the board asserting
        # something the artifacts do not say. The board-native `archived` flag
        # is the OTHER way into this column, for merged cards; both are
        # derived-or-owned, neither is invented.
        return "Archived"
    return None


def deploy_badge(deploy: Optional[dict]) -> dict:
    """The tri-state badge, ruled seq 1391: green = prod matches the mirror,
    amber = merged-not-deployed, red = live-not-merged.

    Computed from `metrics.deploy_state()` and from nothing else (seq 1428 P6).
    Red is the dangerous one and it has two shapes, both of which mean code is
    RUNNING that the mirror has never seen: a prod tree ahead of the mirror, or
    a dirty prod tree. That is the ship-by-not-publishing case, and it must
    never render as merely 'behind'."""
    if not deploy or deploy.get("state") == "unknown":
        return {"badge": "unknown",
                "detail": (deploy or {}).get("detail")
                or "deploy state not configured",
                "state": "unknown"}
    state = deploy.get("state")
    ahead = deploy.get("ahead") or 0
    behind = deploy.get("behind") or 0
    if state == "in-sync":
        return {"badge": "green", "detail": deploy.get("detail", ""),
                "state": state, "ahead": ahead, "behind": behind}
    if deploy.get("dirty") or ahead > 0:
        return {"badge": "red", "detail": deploy.get("detail", ""),
                "state": state, "ahead": ahead, "behind": behind}
    return {"badge": "amber", "detail": deploy.get("detail", ""),
            "state": state, "ahead": ahead, "behind": behind}


def _card(**kw) -> dict:
    base = {
        "slug": "", "kind": "spec", "title": "", "column": "Backlog",
        "spec_path": None, "status": None, "status_word": None,
        "tier": None, "tier_note": None, "lane": None, "review_owner": None,
        "builder": None, "requester": None, "cross_lane": False,
        "confirm_seq": None, "branch": None, "shas": [], "flags": [],
        "deploy": None, "whose_move": "residents", "opened_at": None,
        "updated_at": None, "note": "", "where": "",
    }
    base.update(kw)
    return base


def derive_cards(config: Optional[dict] = None, *, repo: Optional[Path] = None,
                 gatehouse: Optional[Path] = None,
                 message_db: Optional[str] = None,
                 drift: Optional[dict] = None,
                 lane_owners: Optional[dict] = None,
                 date: Optional[str] = None) -> dict:
    """Every card, plus the board's face. Derives; owns nothing; never raises.

    `config` is the parsed broker.toml. Everything else is an override for
    tests and for the keyboard, which reads its own checkout rather than the
    mirror."""
    b = board()
    m = metrics()
    cfg = config or {}
    paths = m.gate_paths(cfg)
    notes: list[str] = []

    repo = Path(repo) if repo else Path(paths.get("mirror") or b.REPO)
    gatehouse = Path(gatehouse) if gatehouse else b.GATEHOUSE
    message_db = message_db or paths.get("message_db")
    lane_owners = {str(k).lower(): str(v)
                   for k, v in (lane_owners or {}).items()}
    date = date or _now().date().isoformat()

    if drift is None:
        if paths.get("configured"):
            try:
                drift = m.gate_drift(cfg, date=date)
            except Exception as exc:  # noqa: BLE001 — a board that dies on the
                # drift detector reports nothing at all, which is worse than a
                # board that reports everything else and says this bit failed.
                drift = None
                notes.append(f"gate drift unavailable: {type(exc).__name__}: {exc}")
        else:
            notes.append(
                "the keyboard-lane gate is not configured in broker.toml "
                "([gate].canonical_repo + [gate].mirror), so the Review column "
                "cannot show uncited `main` commits and the deploy badge reads "
                "`unknown`. An empty Review column and a disarmed detector must "
                "not read alike.")
    drift = drift or {}

    deploy = drift.get("deploy")
    if deploy is None and paths.get("mirror") and paths.get("deploy_tree"):
        deploy = m.deploy_state(mirror=paths["mirror"],
                                deploy_tree=paths["deploy_tree"],
                                branch=paths.get("branch", "main"))
    badge = deploy_badge(deploy)

    specs_dir = repo / "SPECS"
    cards: list[dict] = []

    # -- 1. spec cards. Identity IS the spec file (P5).
    dates = spec_dates(repo)
    merged = b.merged_slugs(repo=repo, gatehouse=gatehouse)
    branches = b.collect_branches(gatehouse=gatehouse)
    by_slug: dict = {}
    for br in branches:
        by_slug.setdefault(br["slug"], []).append(br)

    if not specs_dir.is_dir():
        notes.append(f"no SPECS/ directory at {specs_dir} — nothing to derive")

    for f in sorted(specs_dir.glob("*.md")) if specs_dir.is_dir() else []:
        if f.stem in {"README", "TEMPLATE"} or f.stem.startswith("PASSDOWN"):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        status = brokerd().parse_spec_status(text) or ""
        word = _status_word(status)
        gate = b.spec_gate(f)
        fields = parse_spec_fields(text, f.stem)
        column = column_for_status(word)
        flags: list[str] = []
        whose = "residents"
        note = ""

        if column is None:
            # A Status the gate cannot classify. It is NOT dropped: an
            # unreadable status is the loudest thing a spec can have, and the
            # column that exists for "somebody has to look at this" is Review.
            column = "Review"
            flags.append("unparseable-status")
            whose = "plink"
            note = (f"Status does not parse as any known word "
                    f"({status[:60]!r}) — the gate cannot classify this spec.")

        if word == "confirmed" and not gate["passes"]:
            flags.append("gate-blocked")
            whose = "plink"
            note = ("Says `confirmed`, but the build gate would REFUSE it: "
                    + gate["why"])
        elif word == "confirmed":
            note = "Confirmed and pressable. A resident may start_build now."
        elif word == "draft":
            whose = "plink"
            note = b._clean_status_prose(status) or "Waiting on plink's confirm."
        elif word == "failed":
            flags.append("failed")
            whose = "plink"
            note = ("Last build FAILED. Read its banner in #custodian; to allow "
                    "another attempt set the Status line back to `confirmed`.")
        elif word == "building":
            note = "Build running. Waiting for the reaper's banner."
        elif word.startswith("built@"):
            whose = "plink"
            note = "Built and waiting to be read."
        elif word in MERGED_WORDS:
            whose = "nobody"
            note = "Merged." + (f" Merge commit {merged[f.stem]}."
                                if f.stem in merged else "")
        elif word in DERIVED_ARCHIVE_WORDS:
            whose = "nobody"
            note = f"Done: `{word}`."

        # Stale-status tell: the artifacts say merged, the file does not.
        if f.stem in merged and b._advances_to_merged(word):
            flags.append("stale-status")
            whose = "plink"
            note = (f"Merged as {merged[f.stem]}, but the file still says "
                    f"`{word}` — `board --mark-merged` advances it.")

        parts = by_slug.get(f.stem, [])
        unmerged = [p for p in parts if not p["merged"]]
        branch = f"loop/{f.stem}" if parts else None
        where = f"SPECS/{f.name}"
        if unmerged:
            where += " · " + ", ".join(f"{p['repo']} repo, {p['tip']}"
                                       for p in unmerged)

        d = dates.get(f.stem, {})
        cards.append(_card(
            slug=f.stem, kind="spec", title=fields["title"], column=column,
            spec_path=f"SPECS/{f.name}", status=status, status_word=word,
            tier=fields["tier"], tier_note=fields["tier_note"],
            lane=fields["lane"], review_owner=fields["review_owner"],
            builder=fields["builder"], requester=fields["requester"],
            cross_lane=fields["cross_lane"], confirm_seq=gate["seq"],
            branch=branch, flags=flags,
            deploy=badge if column == "Merged" else None,
            whose_move=whose, opened_at=d.get("opened"),
            updated_at=d.get("updated"), note=note, where=where,
            merge_commit=merged.get(f.stem),
            shortstat="; ".join(p["shortstat"] for p in unmerged
                                if p.get("shortstat")),
        ))

    # -- 2. backlog cards. The only column whose cards may lack a spec file.
    for row in collect_backlog(message_db):
        cards.append(_card(
            slug=f"{BACKLOG_PREFIX}{row['id']}", kind="backlog",
            title=row["title"], column="Backlog",
            requester=row["author"], whose_move="residents",
            opened_at=row["created_at"], updated_at=row["created_at"],
            note="Filed through /backlog. It leaves this column when somebody "
                 "drafts a spec for it.",
            where=f"backlog row {row['id']}, filed by {row['author']}",
            body=row["body"],
        ))

    # -- 3. Review auto-cards: the drift report wearing a UI.
    cards.extend(_keyboard_cards(drift, lane_owners))

    face = {
        "derived_at": _now().isoformat(timespec="seconds"),
        "mirror_head": drift.get("mirror_head") or _head(repo),
        "mirror": str(repo),
        "deploy": badge,
        "schema_version": INDEX_SCHEMA_VERSION,
        "gate_configured": bool(paths.get("configured")),
        "notes": notes,
        "columns": COLUMNS,
        "column_blurbs": COLUMN_BLURBS,
    }
    _order(cards)
    return {"face": face, "cards": cards}


def _head(repo: Path) -> Optional[str]:
    out = board()._run("git", "-C", str(repo), "rev-parse", "HEAD").strip()
    return out or None


def _lane_owner(hits: list, lane_owners: dict) -> Optional[str]:
    """Review owner for a card with no spec file, from the paths it touched.

    Deterministic and CONFIG-DRIVEN: `[planroom].lane_owners` in broker.toml
    maps a path prefix to a name. There is no default map in this file on
    purpose — a lane→owner table is house policy, ruled in #custodian, and a
    guess compiled into a harness module is exactly the hand-written layer this
    build exists to delete. Unmapped reads `unassigned`, which is true."""
    for path in hits:
        for prefix, owner in lane_owners.items():
            if path.lower().startswith(prefix):
                return owner
    return None


def _keyboard_cards(drift: dict, lane_owners: dict) -> list:
    """Auto-cards for the Review column (ruled seq 1391 item 1).

    Two kinds, both derived from Phase 0's push log and neither of which has a
    spec file yet:
      * uncited `main` commits — nothing covered them, or their trailer does
        not resolve. Flagged **uncited**; a Tier 2 among them is the digest's
        LANE VIOLATION, carried here under the same name.
      * keyboard merges whose review is still owed — a push that landed on an
        `override-seq`, or on a `review-seq` the pusher wrote themselves.
    A keyboard card's identity is its shas, not a slug — it has no spec file
    yet. Its EXIT from this column is the retro spec plus the paid review, and
    the card says so, because a card that cannot say how it leaves is a card
    nobody can clear."""
    out: list[dict] = []
    mirror = (drift.get("paths") or {}).get("mirror") or ""

    for c in drift.get("classified", []):
        sha = c.get("sha") or ""
        hits = c.get("hits") or []
        flags = ["uncited"]
        if c.get("tier") == 2:
            flags.append("lane-violation")
        owner = _lane_owner(hits, lane_owners)
        out.append(_card(
            slug=f"{KEYBOARD_PREFIX}{sha[:12]}", kind="keyboard",
            title=(c.get("subject") or sha[:12]).strip(),
            column="Review", shas=[sha], flags=flags,
            review_owner=owner,
            tier=(f"Tier {c['tier']}" if c.get("tier") is not None else None),
            whose_move="plink",
            note=("On `main` and UNCITED — no logged push covered it, or its "
                  "trailer does not resolve. "
                  + ("**LANE VIOLATION**: this is Tier 2. " if c.get("tier") == 2
                     else "")
                  + "It leaves Review when a retro spec exists and the review "
                    "it owes has been paid."),
            where=f"{mirror or 'mirror'} {sha[:12]}"
                  + (f" — touches {', '.join(hits[:4])}" if hits else ""),
            guarded_paths=hits,
        ))

    seen = {c["slug"] for c in out}
    for cit in drift.get("citations", []):
        if not cit.get("holds"):
            continue
        pending = cit.get("kind") == "override-seq" or cit.get("self_cited")
        if not pending:
            continue
        push = cit.get("push") or {}
        new = str(push.get("new") or "")
        slug = f"{KEYBOARD_PREFIX}{new[:12]}"
        if not new or slug in seen:
            continue
        seen.add(slug)
        owner = cit.get("author") or None
        flags = ["review-pending"]
        if cit.get("self_cited"):
            flags.append("self-cited")
        kind = "override" if cit.get("kind") == "override-seq" else "self-cited review"
        out.append(_card(
            slug=slug, kind="keyboard",
            title=f"merged — review: pending {owner or 'unassigned'}",
            column="Review", shas=[new], flags=flags,
            review_owner=owner, confirm_seq=cit.get("seq"),
            whose_move="plink",
            note=(f"Merged from the keyboard on an {kind} (seq "
                  f"{cit.get('seq')}). Review is still owed within a day; "
                  "overrides are counted forever. It leaves Review when the "
                  "review lands."),
            where=f"{mirror or 'mirror'} {new[:12]}",
        ))
    return out


def _order(cards: list) -> None:
    """Derivation order inside a column: whose-move first, then oldest first.

    board_html.py's information design, carried over (spec, Architecture
    notes). The organising question is not "what exists" but "what is waiting
    for a human", so a card waiting on plink sorts above one the residents
    already have, in every column, always. Board-native `sort_order` overrides
    this per card at the server; this is the default a card gets before anybody
    has dragged anything."""
    rank = {"plink": 0, "residents": 1, "nobody": 2}
    cards.sort(key=lambda c: (COLUMNS.index(c["column"])
                              if c["column"] in COLUMNS else len(COLUMNS),
                              rank.get(c["whose_move"], 1),
                              c.get("opened_at") or "",
                              c["slug"]))
    per: dict = {}
    for c in cards:
        n = per.get(c["column"], 0)
        c["position"] = n
        per[c["column"]] = n + 1


# ── the index (a CACHE, never a source) ─────────────────────────────────────

_INDEX_DDL = """
CREATE TABLE face (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE cards (
    slug          TEXT PRIMARY KEY,
    board_column  TEXT NOT NULL,
    position      INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    whose_move    TEXT NOT NULL,
    haystack      TEXT NOT NULL,
    card_json     TEXT NOT NULL
);
CREATE INDEX idx_cards_column ON cards(board_column, position);
"""


def _haystack(card: dict) -> str:
    bits = [card.get("slug"), card.get("title"), card.get("note"),
            card.get("where"), card.get("status"), card.get("lane"),
            card.get("review_owner"), card.get("builder"), card.get("tier"),
            card.get("requester"), card.get("body"), card.get("spec_path"),
            " ".join(card.get("flags") or []),
            " ".join(card.get("shas") or [])]
    return "\n".join(str(b) for b in bits if b)


def build_index(index_path: str, boarddata: dict) -> str:
    """Write the derived index. Rebuilt FROM ZERO every time, by construction.

    There is no incremental path and there will not be one: an index that can
    only be updated is an index that can drift, and drift is the entire defect
    class this build exists to close. The write is atomic (temp file +
    os.replace) so a reader never sees a half-built board, and a rebuild that
    dies leaves the previous index intact rather than a truncated one.

    CACHE, NEVER SOURCE. Git wins every disagreement. Delete this file and the
    next rebuild restores it whole."""
    path = Path(index_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    if tmp.exists():
        tmp.unlink()
    db = sqlite3.connect(str(tmp))
    try:
        db.executescript(_INDEX_DDL)
        face = boarddata["face"]
        db.executemany("INSERT INTO face (key, value) VALUES (?, ?)",
                       [(k, json.dumps(v)) for k, v in face.items()])
        db.executemany(
            "INSERT INTO cards (slug, board_column, position, kind, title, "
            "whose_move, haystack, card_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(c["slug"], c["column"], c.get("position", 0), c["kind"],
              c["title"], c["whose_move"], _haystack(c), json.dumps(c))
             for c in boarddata["cards"]])
        db.commit()
    finally:
        db.close()
    os.replace(tmp, path)
    return str(path)


def read_index(index_path: str) -> dict:
    """The index, back as `{face, cards}`. Missing or unreadable is NOT an
    error and NOT silence: the face says so, and every renderer prints it. An
    absent index and an empty board must not read alike."""
    path = Path(index_path)
    if not path.exists():
        return {"face": {"available": False,
                         "unavailable_reason": f"no index at {path}",
                         "columns": COLUMNS, "column_blurbs": COLUMN_BLURBS},
                "cards": []}
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        face = {r["key"]: json.loads(r["value"])
                for r in db.execute("SELECT key, value FROM face")}
        cards = [json.loads(r["card_json"]) for r in db.execute(
            "SELECT card_json FROM cards ORDER BY board_column, position")]
        db.close()
    except (sqlite3.Error, ValueError) as exc:
        return {"face": {"available": False,
                         "unavailable_reason": f"index unreadable: {exc}",
                         "columns": COLUMNS, "column_blurbs": COLUMN_BLURBS},
                "cards": []}
    face.setdefault("columns", COLUMNS)
    face.setdefault("column_blurbs", COLUMN_BLURBS)
    face["available"] = True
    return {"face": face, "cards": cards}


# ── transitions (the #custodian notification stream) ────────────────────────

def snapshot(boarddata: dict) -> dict:
    """slug -> column. The whole state a transition is computed from."""
    return {c["slug"]: c["column"] for c in boarddata["cards"]}


def detect_transitions(old: dict, new: dict) -> list:
    """One entry per COLUMN transition — never per edit (spec, Notifications).

    Residents are event-driven, so the stream is their trigger; and because it
    is posted to #custodian it doubles as a witnessable seq trail of the whole
    lifecycle, for free. Per-edit notification would drown both properties in
    the same week."""
    out = []
    for slug, col in new.items():
        was = old.get(slug)
        if was is None:
            out.append({"slug": slug, "from": None, "to": col, "kind": "opened"})
        elif was != col:
            out.append({"slug": slug, "from": was, "to": col, "kind": "moved"})
    for slug, col in old.items():
        if slug not in new:
            out.append({"slug": slug, "from": col, "to": None, "kind": "closed"})
    out.sort(key=lambda t: (t["kind"], t["slug"]))
    return out


def format_transition(t: dict, cards: Optional[dict] = None) -> str:
    """The system line. brief's rule, inherited: never print a bare identifier —
    every line says what the thing is, not just what it is called."""
    card = (cards or {}).get(t["slug"]) or {}
    title = card.get("title") or t["slug"]
    tail = f" — {title}" if title != t["slug"] else ""
    if t["kind"] == "opened":
        return f"plan room: {t['slug']} opened in {t['to']}{tail}"
    if t["kind"] == "closed":
        return f"plan room: {t['slug']} left the board (was {t['from']}){tail}"
    return f"plan room: {t['slug']} {t['from']} → {t['to']}{tail}"


def rebuild(index_path: str, boarddata: dict) -> list:
    """Rebuild the index and return the transition lines the move produced.

    Reads the OLD index before overwriting it — the previous snapshot lives in
    the index and nowhere else, which is the property that makes the detector
    stateless: no separate memory of "what I last announced" to fall out of
    step with what the board actually shows.

    A COLD START ANNOUNCES NOTHING. With no prior index there is no prior
    board, so every card would read as newly `opened` and the first rebuild
    after any install — or after anyone deletes the cache, which they are
    explicitly invited to do — would dump the entire history of the house into
    #custodian as if it had all just happened. "Opened" is a claim about
    movement, and movement needs a before."""
    previous = read_index(index_path)
    cold = previous["face"].get("available") is False
    before = snapshot(previous)
    build_index(index_path, boarddata)
    if cold:
        return []
    after = snapshot(boarddata)
    by_slug = {c["slug"]: c for c in boarddata["cards"]}
    return [format_transition(t, by_slug)
            for t in detect_transitions(before, after)]


# ── terminal rendering (renderer #3: the CLI) ───────────────────────────────

def render_text(boarddata: dict, out=sys.stdout) -> None:
    w = out.write
    face = boarddata["face"]
    cards = boarddata["cards"]
    by_col: dict = {}
    for c in cards:
        by_col.setdefault(c["column"], []).append(c)

    w("\n  THE PLAN ROOM\n")
    if face.get("available") is False:
        w(f"     UNAVAILABLE — {face.get('unavailable_reason')}\n\n")
        return
    w(f"     derived {face.get('derived_at', '?')} UTC from mirror "
      f"{(face.get('mirror_head') or '?')[:12]}\n")
    badge = (face.get("deploy") or {}).get("badge", "unknown")
    w(f"     deploy: {badge.upper()} — {(face.get('deploy') or {}).get('detail', '')}\n")
    for n in face.get("notes", []):
        w(f"     note: {n}\n")
    w("\n")

    for col in face.get("columns", COLUMNS):
        items = by_col.get(col, [])
        w(f"  {col.upper()}  ({len(items)})\n")
        if not items:
            w("     —\n\n")
            continue
        for c in items:
            mark = {"plink": "!", "residents": "·", "nobody": " "}.get(
                c["whose_move"], " ")
            flags = f"  [{', '.join(c['flags'])}]" if c["flags"] else ""
            w(f"   {mark} {c['title']}{flags}\n")
            if c.get("note"):
                w(f"       {c['note']}\n")
            w(f"       where: {c.get('where') or c['slug']}\n")
        w("\n")


def main(argv=None) -> int:
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="/etc/disjorn-broker/broker.toml",
                    help="broker.toml (for [gate] and [planroom])")
    ap.add_argument("--index", help="index path (default: [planroom].index)")
    ap.add_argument("--json", action="store_true", help="emit data, not a view")
    ap.add_argument("--rebuild", action="store_true", help="write the index")
    ap.add_argument("--transitions", action="store_true",
                    help="with --rebuild: print the #custodian lines")
    ap.add_argument("--read", action="store_true",
                    help="render the INDEX rather than deriving afresh")
    ns = ap.parse_args(argv)

    config: dict = {}
    if os.path.exists(ns.config):
        import tomllib
        with open(ns.config, "rb") as fh:
            config = tomllib.load(fh)
    pr = config.get("planroom", {}) if isinstance(config.get("planroom"), dict) else {}
    index_path = ns.index or pr.get("index")

    if ns.read:
        if not index_path:
            print("no index path: pass --index or set [planroom].index",
                  file=sys.stderr)
            return 2
        data = read_index(index_path)
    else:
        data = derive_cards(config, lane_owners=pr.get("lane_owners"))

    if ns.rebuild:
        if not index_path:
            print("no index path: pass --index or set [planroom].index",
                  file=sys.stderr)
            return 2
        lines = rebuild(index_path, data)
        if ns.transitions:
            for ln in lines:
                print(ln)
        else:
            print(f"wrote {index_path}: {len(data['cards'])} cards, "
                  f"{len(lines)} transition(s)")
        return 0

    if ns.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    render_text(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
