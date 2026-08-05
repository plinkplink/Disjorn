#!/usr/bin/env python3
"""brief.py — "what happened since you left", on one screen, for plink.

THE PROBLEM THIS EXISTS FOR, in his words (2026-08-04): *"I don't know what or
where BL-D7, KB-D6, H13-D7 and KB-D1 are. This is most of the problem — agents
referring to obscure items and locations. By the time I get around to finding
what they are, ten new decisions are in committee."*

So the hard rule here is: **THIS TOOL NEVER PRINTS A BARE ITEM CODE.** Every
`BL-D7` carries its one-line meaning and its file:line, expanded automatically
from wherever it is defined. If a code cannot be resolved, the brief says so
loudly rather than printing it naked — an unexplained code is the bug, and a
silent one is the bug hiding.

The second rule: **this file is GENERATED, never maintained.** The house
already has twenty-one hand-written markdown files totalling ~4,900 lines, and
the cost of re-reading them on every return is the actual bottleneck. Adding a
twenty-second maintained document would make it worse. Everything below is read
live from things that cannot go stale without something else breaking first:
git, the broker audit ledger, the house error log, systemd, and the Disjorn
database.

Usage:
    brief.py                  # since your last brief (or 3d on first run)
    brief.py --since 7d       # explicit window; does not move the watermark
    brief.py --mark           # move the watermark to now (do this when done)
    brief.py what BL-D7 KB-D6 # expand codes and exit
    brief.py glossary         # every code the house has defined
    brief.py seq              # recent #custodian messages WITH seq numbers
    brief.py seq --mine       # only plink's — "what seq was my nod?"
    brief.py seq --grep tiers # find the seq of a message by its text
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
DB = REPO / "server" / "data" / "disjorn.db"
ERRORLOG = Path("/var/log/disjorn-errorlog/errors.jsonl")
AUDIT = Path("/var/log/disjorn-broker/audit.jsonl")
STATE = Path(os.environ.get("DISJORN_BRIEF_STATE",
                            Path.home() / ".disjorn-brief-state.json"))
CUSTODIAN = 4

# Docs searched for code definitions, best source first. Order matters: the
# first doc that defines a code richly wins, so the canonical backlogs beat a
# passing mention in a status roll-up.
GLOSSARY_DOCS = [
    "RED-TEAM-BACKLOG.md", "DEFERRED.md", "BUILD-LOOP.md", "HARNESS-PLAN.md",
    "harness/KEYBOARD-NEXT.md", "STATUS.md", "AGENTHOOD.md", "AUTHORITY-PLAN.md",
    "Architecture.md", "MEMORY-DESIGN.md", "BUILD-PLAN.md",
]
CODE_RE = re.compile(r"\b((?:BL|KB|H13|PK|WP|DM|MG)-[A-Z]?\d+[a-z]?)\b")

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, YEL, GRN, CYA = "\033[31m", "\033[33m", "\033[32m", "\033[36m"


def _c(s, code):
    return s if not sys.stdout.isatty() else f"{code}{s}{RESET}"


# ── glossary ────────────────────────────────────────────────────────────────

def build_glossary() -> dict:
    """{code: {gloss, file, line, open, rank}} from the markdown the house
    already writes. Parsed rather than curated, so a new backlog item is
    explainable the moment someone files it — a curated glossary would be doc
    twenty-two and would rot on exactly the schedule as the rest.

    Codes get defined in three shapes and they are NOT equally authoritative,
    so each carries an explicit rank and the best one wins:

      2  a section heading  (`## WP-A1 — the broker gets its own uid`)
         The canonical definition. WP-A1's only real explanation is a heading,
         and reading bullets alone glossed it from a passing mention that read
         "delivers everything it promised" — true, and useless to someone who
         does not already know what it is.
      1  a checkbox backlog item (`- [ ] **BL-D7 (HIGH)** — ...`)
         Carries open/closed state, which no other shape does.
      0  a bold bullet (`- **BL-G1** — ...`)

    Ties inside a rank go to the longer text. Rank always beats length: a terse
    heading is still a better definition than a verbose aside."""
    out: dict[str, dict] = {}

    def offer(code, gloss, rel, line, is_open, rank):
        gloss = re.sub(r"\s+", " ", gloss).strip(" —-:*")
        if not gloss:
            return
        prev = out.get(code)
        if prev is None or rank > prev["rank"] or \
                (rank == prev["rank"] and len(gloss) > len(prev["gloss"])):
            out[code] = {"gloss": gloss, "file": rel, "line": line,
                         "open": is_open if is_open is not None else (
                             prev["open"] if prev else None),
                         "rank": rank}

    for rel in GLOSSARY_DOCS:
        path = REPO / rel
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            # -- shape 2: a heading
            h = re.match(r"\s*#{2,4}\s+`?([A-Z0-9]+-[A-Z]?\d+[a-z]?)`?\b(.*)", line)
            if h:
                code, rest = h.groups()
                tail = [re.sub(r"\*\(.*?\)\*", "", rest)]
                for cont in lines[i + 1:i + 4]:
                    if not cont.strip() or cont.startswith("#") or \
                            re.match(r"\s*[-*]\s*\[", cont):
                        break
                    tail.append(cont.strip())
                offer(code, " ".join(tail), rel, i + 1, None, 2)
                continue
            # -- shapes 1 and 0: a bullet, optionally with a checkbox
            m = re.match(r"\s*(?:[-*]\s*)?(?:\[([ x])\]\s*)?\*\*([A-Z0-9]+-[A-Z]?\d+[a-z]?)\b(.*)", line)
            if not m:
                continue
            checked, code, rest = m.groups()
            rest = re.sub(r"^\s*\([^)]*\)", "", rest)
            rest = rest.replace("**", "").lstrip(" —-:")
            body = [rest.strip()]
            for cont in lines[i + 1:i + 4]:
                if not cont.strip() or re.match(r"\s*(?:[-*]\s*)?(?:\[[ x]\])?\s*\*\*", cont):
                    break
                body.append(cont.strip())
            offer(code, " ".join(x for x in body if x), rel, i + 1,
                  None if checked is None else (checked == " "),
                  1 if checked is not None else 0)
    return out


# Abbreviations whose trailing dot is not a sentence end. Without these the
# gloss for H13-D7 stops at "(e.g." and explains nothing, which is the exact
# failure this tool exists to prevent.
_ABBREV = ("e.g.", "i.e.", "cf.", "vs.", "etc.", "approx.", "Dr.", "No.")


def first_sentence(text: str, cap: int = 190) -> str:
    """One readable line. Markdown emphasis is stripped rather than passed
    through — a gloss printed to a terminal should read as prose, not as the
    source it was lifted from."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\s+", " ", text).strip()
    search = text[:cap + 80]
    for i in range(len(search)):
        if search[i] not in ".;":
            continue
        if i + 1 < len(search) and search[i + 1] not in " \t":
            continue
        if any(search[:i + 1].endswith(a) for a in _ABBREV):
            continue
        if i + 1 >= 40:  # don't stop on a fragment
            text = text[:i + 1]
            break
    return text if len(text) <= cap else text[:cap].rstrip() + "…"


def expand(codes, glossary, indent="   ") -> list[str]:
    lines = []
    for code in codes:
        e = glossary.get(code)
        if e is None:
            lines.append(f"{indent}{_c(code, RED)} — {_c('NOT DEFINED ANYWHERE', RED)}. "
                         f"An unexplained code is the bug; go name it.")
            continue
        state = ""
        if e["open"] is True:
            state = _c(" [OPEN]", YEL)
        elif e["open"] is False:
            state = _c(" [closed]", GRN)
        lines.append(f"{indent}{_c(code, BOLD)}{state} — {first_sentence(e['gloss'])}")
        lines.append(f"{indent}{_c(e['file'] + ':' + str(e['line']), DIM)}")
    return lines


def codes_in(text: str) -> list[str]:
    seen, out = set(), []
    for m in CODE_RE.finditer(text):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


# ── sources ─────────────────────────────────────────────────────────────────

def git_since(since_iso: str) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "log", f"--since={since_iso}",
             "--pretty=format:%h\x1f%s\x1f%ad", "--date=format:%m-%d %H:%M"],
            capture_output=True, text=True, timeout=15)
        return [l for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def channel_since(since_iso: str) -> list[dict]:
    if not DB.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT seq, author_type, author_id, created_at, content FROM messages "
            "WHERE channel_id=? AND deleted_at IS NULL AND created_at > ? ORDER BY seq",
            (CUSTODIAN, since_iso)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _actor_names() -> dict:
    """{('user'|'bot', id): name} so the listing shows who said it, not a
    numeric id. The seq is the thing being looked up; an unreadable author
    column would just move the lookup problem somewhere else."""
    names: dict = {}
    if not DB.exists():
        return names
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        for t, q in (("user", "SELECT id, username FROM users"),
                     ("bot", "SELECT id, name FROM bots")):
            for i, n in db.execute(q):
                names[(t, i)] = n
    except Exception:
        pass
    return names


def recent_messages(limit: int, channel: int, mine: bool, grep) -> list[dict]:
    """Newest-last, so the most recent seq is the last line on screen and does
    not scroll away — this exists to be read right after posting."""
    if not DB.exists():
        return []
    sql = ("SELECT seq, author_type, author_id, created_at, content FROM messages "
           "WHERE channel_id=? AND deleted_at IS NULL")
    args: list = [channel]
    if mine:
        sql += " AND author_type='user'"
    if grep:
        sql += " AND content LIKE ?"
        args.append(f"%{grep}%")
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return [dict(r) for r in reversed(db.execute(sql, args).fetchall())]
    except Exception:
        return []


def errors_since(since_iso: str) -> list[dict]:
    if not ERRORLOG.exists():
        return []
    out = []
    try:
        for line in ERRORLOG.read_text(errors="replace").splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            stamp = d.get("ts") or d.get("logged_at") or ""
            if stamp > since_iso:
                out.append(d)
    except PermissionError:
        return []
    return out


def timers() -> list[tuple[str, str, str]]:
    try:
        r = subprocess.run(
            ["systemctl", "list-timers", "--all", "--no-pager", "--output=json"],
            capture_output=True, text=True, timeout=15)
        rows = json.loads(r.stdout or "[]")
    except Exception:
        return []
    out = []
    for t in rows:
        unit = t.get("unit", "")
        if "disjorn" not in unit and "claudette" not in unit:
            continue
        nxt = t.get("next") or t.get("NextElapseUSecRealtime")
        when = "—"
        if isinstance(nxt, (int, float)) and nxt > 0:
            when = datetime.fromtimestamp(nxt / 1e6).strftime("%m-%d %H:%M")
        out.append((unit, when, t.get("activates", "")))
    return sorted(out)


def waiting_on_plink() -> list[str]:
    """Rows of STATUS.md's 'Waiting on plink' table, verbatim question text."""
    path = REPO / "STATUS.md"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out, inside = [], False
    for line in lines:
        if line.startswith("## Waiting on plink"):
            inside = True
            continue
        if inside:
            if line.startswith("## "):
                break
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 4 and cells[0].isdigit():
                # Strip markdown: this is read in a terminal, not rendered.
                q = re.sub(r"\*\*([^*]*)\*\*", r"\1", cells[1])
                q = q.replace("**", "").replace("*", "").replace("`", "")
                out.append(f"{q}  ({_c('asked by ' + cells[2] + ', since ' + cells[3], DIM)})")
    return out


# ── render ──────────────────────────────────────────────────────────────────

def parse_since(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([hdw])", text.strip())
    if not m:
        raise SystemExit(f"--since wants forms like 12h, 3d, 2w (got {text!r})")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(hours=n) if unit == "h" else \
        timedelta(days=n) if unit == "d" else timedelta(weeks=n)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def main() -> int:
    # `brief glossary | head` is the obvious thing to type and must not end in
    # a BrokenPipeError traceback. Restoring the default SIGPIPE handling makes
    # a truncated pipe end the process quietly, as every other unix tool does.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass  # not POSIX, or not the main thread

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?",
                    choices=["brief", "what", "glossary", "seq"], default="brief")
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--mine", action="store_true",
                    help="seq: only plink's own messages")
    ap.add_argument("--grep", default=None,
                    help="seq: only messages containing this text")
    ap.add_argument("--limit", type=int, default=15, help="seq: how many (default 15)")
    ap.add_argument("--channel", type=int, default=CUSTODIAN,
                    help=f"seq: channel id (default {CUSTODIAN} = #custodian)")
    ap.add_argument("--since", default=None, help="window like 12h / 3d / 2w")
    ap.add_argument("--mark", action="store_true", help="move the watermark to now")
    ns = ap.parse_args()

    glossary = build_glossary()

    if ns.cmd == "what":
        if not ns.codes:
            raise SystemExit("usage: brief.py what BL-D7 KB-D6")
        print()
        for line in expand([c.upper() for c in ns.codes], glossary, indent=" "):
            print(line)
        print()
        return 0

    if ns.cmd == "seq":
        rows = recent_messages(ns.limit, ns.channel, ns.mine, ns.grep)
        if not rows:
            print("\nno messages matched\n")
            return 0
        names = _actor_names()
        print()
        for m in rows:
            who = names.get((m["author_type"], m["author_id"]),
                            f"{m['author_type']}{m['author_id']}")
            when = (m["created_at"] or "")[11:16]
            head = re.sub(r"[*`#>]", "", m["content"] or "").strip()
            head = re.sub(r"\s+", " ", head)[:64]
            print(f"  {_c('seq ' + str(m['seq']).rjust(4), BOLD)}  "
                  f"{_c(when, DIM)}  {who:<13} {head}")
        print(f"\n  {_c('newest last. paste the seq into the Confirm record.', DIM)}\n")
        return 0

    if ns.cmd == "glossary":
        openc = sum(1 for e in glossary.values() if e["open"])
        print(f"\n{len(glossary)} codes defined, {openc} open\n")
        for code in sorted(glossary):
            for line in expand([code], glossary, indent=" "):
                print(line)
        print()
        return 0

    now = datetime.now(timezone.utc)
    state = load_state()
    if ns.since:
        since = now - parse_since(ns.since)
        window = f"last {ns.since}"
    elif state.get("last"):
        since = datetime.fromisoformat(state["last"])
        gap = now - since
        hours = gap.total_seconds() / 3600
        window = f"since your last brief, {int(hours)}h ago" if hours < 48 \
            else f"since your last brief, {gap.days}d ago"
    else:
        since = now - timedelta(days=3)
        window = "last 3d (no previous brief on record)"
    since_iso = since.isoformat()

    commits = git_since(since_iso)
    msgs = channel_since(since_iso.replace("+00:00", "Z"))
    errs = errors_since(since_iso.replace("+00:00", "Z"))
    waiting = waiting_on_plink()

    # Every code mentioned anywhere in what we are about to print, so the
    # glossary at the bottom covers exactly this brief and nothing else.
    mentioned: list[str] = []
    for blob in [" ".join(commits), " ".join(w for w in waiting),
                 " ".join(m["content"] for m in msgs)]:
        for c in codes_in(blob):
            if c not in mentioned:
                mentioned.append(c)

    W = 78
    print()
    print(_c("═" * W, CYA))
    print(_c(f" DISJORN — {window}", BOLD))
    print(_c("═" * W, CYA))

    print(f"\n{_c('WAITING ON YOU', BOLD)}")
    if waiting:
        for w in waiting:
            print(f"   • {w}")
    else:
        print(f"   {_c('nothing recorded', DIM)}")

    print(f"\n{_c('WHAT CHANGED', BOLD)}  ({len(commits)} commits)")
    for c in commits[:14]:
        h, subject, when = (c.split("\x1f") + ["", ""])[:3]
        print(f"   {_c(when, DIM)}  {_c(h, CYA)}  {subject[:64]}")
    if len(commits) > 14:
        print(f"   {_c(f'… and {len(commits) - 14} more', DIM)}")
    if not commits:
        print(f"   {_c('nothing committed', DIM)}")

    print(f"\n{_c('WHAT BROKE', BOLD)}  ({len(errs)} events)")
    if errs:
        by_kind: dict[str, int] = {}
        for e in errs:
            by_kind[e.get("kind", "other")] = by_kind.get(e.get("kind", "other"), 0) + 1
        for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            colour = RED if kind in ("crash", "refusal", "truncation") else YEL
            print(f"   {_c(kind, colour)} x{n}")
        print(f"   {_c('detail: errorlog.py tail --days 3', DIM)}")
    else:
        print(f"   {_c('nothing', DIM)}")

    print(f"\n{_c('SCHEDULED', BOLD)}")
    for unit, when, _ in timers():
        print(f"   {when}  {unit}")

    bots = sum(1 for m in msgs if m["author_type"] == "bot")
    print(f"\n{_c('#custodian', BOLD)}  ({len(msgs)} messages, {bots} from bots)")
    for m in msgs[-6:]:
        who = "you" if m["author_type"] == "user" else f"bot{m['author_id']}"
        head = re.sub(r"[*`#]", "", m["content"]).strip().replace("\n", " ")[:62]
        print(f"   {_c('seq ' + str(m['seq']), DIM)} {who:>5}  {head}")
    if len(msgs) > 6:
        print(f"   {_c(f'… {len(msgs) - 6} earlier', DIM)}")

    if mentioned:
        print(f"\n{_c('CODES MENTIONED ABOVE', BOLD)}  "
              f"{_c('(never look these up again)', DIM)}")
        for line in expand(mentioned, glossary):
            print(line)

    print()
    if ns.mark:
        STATE.write_text(json.dumps({"last": now.isoformat()}, indent=1))
        print(_c(f" watermark moved to now — next brief starts here\n", DIM))
    elif not ns.since:
        print(_c(" run with --mark when you're done to move the watermark\n", DIM))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
