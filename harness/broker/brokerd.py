#!/usr/bin/env python3
"""disjorn-broker — the privileged verb gateway for residents (WP-H3)."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pwd
import re
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Any, Callable, Optional

DEFAULT_CONFIG_PATH = "/etc/disjorn-broker/broker.toml"
DEFAULT_VERBS_PATH = "/etc/disjorn-broker/verbs.toml"
ENV_CONFIG = "DISJORN_BROKER_CONFIG"
ENV_VERBS = "DISJORN_BROKER_VERBS"

DEFAULT_SOCKET_PATH = "/run/disjorn-broker.sock"  # per HARNESS-PLAN; the
# shipped broker.toml template uses /run/disjorn-broker/broker.sock instead so the
# daemon can run unprivileged under systemd RuntimeDirectory=.

MAX_REQUEST_BYTES = 64 * 1024  # one request line; anything bigger is hostile
MAX_PROPOSAL_CHARS = 4000
MAX_LOG_LINES = 500
MAX_AUDIT_ENTRIES = 500
MAX_GREP_CHARS = 200
MAX_GATES_JSON = 8192
# Plan Room (SPECS/2026-08-20-plan-room.md).
MAX_BOARD_CARDS = 200
MAX_BOARD_COMMENT_CHARS = 4000
MAX_BOARD_REASON_CHARS = 500
MAX_BOARD_SEARCH_CHARS = 200
# A card slug, anchored.
BOARD_SLUG_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,50}"
                           r"|backlog-\d{1,12}|keyboard-[0-9a-f]{7,40})$")
PLANROOM_HTTP_TIMEOUT = 20
# How often the daemon re-derives the board when nothing else has triggered it.
DEFAULT_PLANROOM_TIMER_SEC = 900
SUBPROCESS_TIMEOUTS = {  # seconds, per verb
    "restart-disjorn": 60,
    "run-server-tests": 900,
    "classify-diff": 120,
    "read-prod-logs": 30,
    "refresh-mirror": 120,
    "spec-status": 60,
}

START_BUILD_DEFAULT_TIMEOUT = 3600
# Ratified default (BUILD-LOOP.md): builds are CAPPED by default (2/day), unlike the
# WP-H12 action budget which ships OFF. plink tunes at staging time.
DEFAULT_DAILY_BUILD_CAP = 2
MAX_SPEC_BYTES = 64 * 1024
# BL-D2: the detached build's stdout/stderr go to temp FILES (bounded on disk),
# never to a pipe the privileged broker must drain into RAM.
MAX_BUILD_LOG_TAIL = 64 * 1024

_RANGE_RE = re.compile(r"^[A-Za-z0-9._~^/{}-]{1,200}$")  # git rev / range; no
# whitespace, no leading dash (checked separately) — can never be read as a flag.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SPEC_STEM_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9][a-z0-9-]{0,50})$")

BUILD_UNIT_PREFIX = "disjorn-build-"
# Unit states that mean "this build is still going".
BUILD_ACTIVE_STATES = frozenset(
    {"active", "activating", "deactivating", "reloading", "refreshing"})
# One JSON sidecar per in-flight build, written next to its output spool BEFORE the
# launch.
BUILD_SIDECAR_SUFFIX = ".build.json"
BUILD_SIDECAR_SCHEMA = 1

# --------------------------------------------------------------- apps-build
# SPECS/2026-09-06-apps-builder-seat.md §B/§E. An APP BUILD TURN is launched the
# same way a spec build is — `sudo -n <launcher> run …`, a transient unit under a
# seat's uid, a 0600 sidecar so a broker restart can re-adopt it — but everything
# the broker learns about the turn comes back through the FILESYSTEM
# (`/srv/apps-turns/<session>/<turn>/result.json`, written atomically by the seat's
# harvest) rather than through the spool.
APPS_UNIT_PREFIX = "disjorn-apps-"
APPS_SIDECAR_SUFFIX = ".apps.json"
APPS_SIDECAR_SCHEMA = 1
# The launcher's "refused before any privilege" exit (bad charset, prompt outside
# the mapped dir, symlink, wrong owner, over the byte bound).
APPS_LAUNCH_REFUSED_EXIT = 64
APPS_SPAWN_CHECK_SEC = 1.0
# The stage endpoint's own bound on `detail` (server-side, 2000 chars of JSON).
APPS_DETAIL_MAX_CHARS = 2000
APPS_FILES_CAP = 40
APPS_SUMMARY_MAX = 300
APPS_TURN_MAX_SEC = 1800          # launch.toml [apps].turn_max_sec, mirrored
# What `[apps]` means when a key is absent.
APPS_DEFAULTS: dict = {
    "runner": "claude-code",
    "seat_bots": {},
    "model": "claude-opus-5",
    "build_token_ceiling": 10_000_000,
    "prompt_max_bytes": 65536,
    "turns_root": "/srv/apps-turns",
    "launch_command": ["sudo", "-n",
                       "/usr/local/lib/disjorn/disjorn-apps-launch", "run"],
    # Slice (iv): the user's stop.
    "stop_command": ["sudo", "-n",
                     "/usr/local/lib/disjorn/disjorn-apps-launch", "stop"],
    # How often a live turn's reaper asks harness-view whether the owner has pressed
    # Stop.
    "stop_poll_sec": 5,
    "unit_state_command": ["systemctl", "show", "--property=ActiveState",
                           "--value"],
    "ledger_path": "/var/log/disjorn-broker/apps-ledger.jsonl",
    "log_dir": "/var/log/disjorn-broker/apps-logs",
    "poll_sec": 2,
    "result_grace_sec": 30,
    # systemd's own TimeoutStopSec default.
    "unit_stop_timeout_sec": 90,
    "chat_markers": ["[[CHAT]]", "[[/CHAT]]"],
}
APPS_APP_ID_RE = re.compile(r"^[a-z2-7]{12}$")
# The launcher's own refusal code (its EXIT_REFUSED): a shape or path it would not
# act on, before any privilege.
APPS_LAUNCH_REFUSED = 64
# How many launcher refusals a stop request survives before the reaper stops asking.
APPS_STOP_MAX_REFUSALS = 3
APPS_SEAT_RE = re.compile(r"^res-[a-z]{1,24}$")
_APPS_MORE_RE = re.compile(r"^\+(\d+) more$")
# The one sentence a resident that faithfully quoted a user gets to say back (§E).
APPS_CHAT_MARKER_REFUSAL = (
    "The prompt file contains a chat marker the harness cannot pass through; "
    "quote the user's words without it")


def apps_unit_name(session: int, turn: int) -> str:
    """The transient unit one turn runs in."""
    return f"{APPS_UNIT_PREFIX}{int(session)}-{int(turn)}.service"


def apps_tokens(usage: Optional[dict]) -> int:
    """The ceiling column: input + output + cache_creation (spec §E "Ledger")."""
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
    return total


def apps_clean_line(text: Any, limit: int = APPS_SUMMARY_MAX) -> Optional[str]:
    """One line of runner-written text, safe to put in a room."""
    if not isinstance(text, str):
        return None
    cleaned = "".join(ch for ch in text if ch == " " or ch.isprintable()).strip()
    return cleaned[:limit] or None


def apps_cap_files(files: Any, cap: int = APPS_FILES_CAP) -> list[str]:
    """The turn's file list, bounded."""
    if not isinstance(files, list):
        return []
    names = [str(f) for f in files if isinstance(f, str)]
    if len(names) <= cap:
        return names
    return names[:cap] + [f"+{len(names) - cap} more"]


def apps_fit_detail(detail: dict) -> dict:
    """A stage `detail` that will fit the server's 2000-char bound, whatever the
    turn touched."""
    fitted = dict(detail)
    if isinstance(fitted.get("summary"), str):
        fitted["summary"] = fitted["summary"][:APPS_SUMMARY_MAX]

    def size(d: dict) -> int:
        return len(json.dumps(d, ensure_ascii=False))

    if size(fitted) <= APPS_DETAIL_MAX_CHARS:
        return fitted
    if "summary" in fitted:
        del fitted["summary"]
    files = fitted.get("files")
    if not isinstance(files, list):
        return fitted
    named = [f for f in files if not _APPS_MORE_RE.match(str(f))]
    dropped = sum(int(m.group(1)) for m in
                  (_APPS_MORE_RE.match(str(f)) for f in files) if m)
    while size(fitted) > APPS_DETAIL_MAX_CHARS and named:
        cut = max(1, len(named) // 2)
        named, dropped = named[:-cut], dropped + cut
        fitted["files"] = named + [f"+{dropped} more"]
    return fitted

# ---------------------------------------------------------------- publish lines
# SPECS/2026-08-13-build-publish-path.md item 3. The build session no longer pushes
# anything: after the container exits, run-build.sh harvests HOST-side (as
# res-<name>, where the gatehouse group actually exists) and prints one
# machine-readable line per entitled repo.
_PUBLISH_REPO_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_PUBLISH_LINE_RES = (
    ("published", re.compile(
        rf"^PUBLISHED[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]+([0-9a-fA-F]{{7,64}})"
        r"[ \t]*$")),
    ("failed", re.compile(
        rf"^PUBLISH-FAILED[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]+(\S.*)$")),
    ("no_commits", re.compile(
        rf"^NO-COMMITS[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]*$")),
    # The quarantine line names the repo WITHOUT .git (it is a workspace clone, not
    # a bare repo) and carries a path we only ever echo, never open.
    ("quarantined", re.compile(
        rf"^QUARANTINED[ \t]+({_PUBLISH_REPO_RE})[ \t]+(\S.*)$")),
)
# Bounds on what reaches the banner.
MAX_PUBLISH_LINES = 8
MAX_PUBLISH_ERR_CHARS = 200
MAX_QUARANTINE_PATH_CHARS = 160

# ---------------------------------------------------------------------- wake
# SPECS/2026-08-25-agentic-residents.md. A wake starts a headless work session in a
# resident's seat.
WAKE_VERB = "wake"
MAX_WAKE_TASK_CHARS = 4000
# One record per wake, in the plink-owned spool.
WAKE_SPOOL_SUFFIX = ".wake.json"
WAKE_SPOOL_SCHEMA = 1
# Wall-clock cap for a woken session, in seconds.
DEFAULT_WAKE_SESSION_CAP_SEC = 5400
# How long after the cap a wake is still considered in flight.
DEFAULT_WAKE_GRACE_SEC = 600
# How long a record stays in the spool after its window closes.
WAKE_RETENTION_SEC = 7 * 86400
# Wakes per seat per UTC day, CAPPED BY DEFAULT — an unset cap is not "no policy",
# it is an unbounded number of 5400s account-billed sessions behind one button.
DEFAULT_DAILY_WAKE_CAP = 3
_WAKE_ID_RE = re.compile(r"^wake-\d{8}T\d{6}Z-[0-9a-f]{6}$")


def build_unit_name(slug: str) -> str:
    """`2026-07-21-gif-picker` -> `disjorn-build-2026-07-21-gif-picker.service`."""
    m = _SPEC_STEM_RE.match(slug) if isinstance(slug, str) else None
    if not m:
        raise _bad(f"slug is not a valid spec stem: {slug!r}")
    try:
        _dt.date.fromisoformat(m.group(1))
    except ValueError:
        raise _bad(f"slug date is not a real date: {slug!r}") from None
    return f"{BUILD_UNIT_PREFIX}{slug}.service"


class VerbError(Exception):
    """A verb failed or a request was rejected. code -> PROTOCOL.md error codes."""

    def __init__(self, code: str, message: str,
                 status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _bad(msg: str) -> VerbError:
    return VerbError("bad-args", msg)


class ConfigError(Exception):
    """Broker configuration is unsafe."""


# --------------------------------------------------------------------------
# BL-D1 — the start-build authorization surface, enforced instead of commented.
# --------------------------------------------------------------------------

def _resident_gids(uid: int) -> set[int]:
    """Every gid a uid belongs to (primary + supplementary)."""
    try:
        pw = pwd.getpwuid(uid)
    except KeyError:
        return set()
    gids = {pw.pw_gid}
    try:
        gids.update(os.getgrouplist(pw.pw_name, pw.pw_gid))
    except (OSError, KeyError):  # pragma: no cover — libc/nss failure
        pass
    return gids


def _is_within(path: str, root: str) -> bool:
    """True if `path` IS `root` or sits underneath it."""
    if path == root:
        return True
    return path.startswith(root.rstrip("/") + "/")


def _path_components(path: str) -> list[str]:
    """`/a/b/c` -> ['/a/b/c', '/a/b', '/a', '/'] — the leaf first, then every parent
    up to the root, so a caller can stat the whole chain."""
    out = [path]
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            break
        out.append(parent)
        path = parent
    return out


def assert_specs_dir_resident_unwritable(
    specs_dir: str,
    *,
    uid_map: dict[int, str],
    residents: dict[str, dict],
    broker_uid: Optional[int] = None,
    gids_for_uid: Callable[[int], set[int]] = _resident_gids,
) -> str:
    """Enforce the BL-D1 invariant (see the block comment above) or raise
    ConfigError naming the offending path."""
    return assert_dir_resident_unwritable(
        specs_dir,
        label="start_build.specs_dir",
        remedy=("The confirm gate is only meaningful when SPECS/ is the "
                "plink-gated read-only mirror; point specs_dir there (e.g. "
                "/srv/disjorn-ro/SPECS)."),
        stake=("A resident that can write any component of SPECS/ can forge "
               "its own confirm record and self-authorize a build."),
        uid_map=uid_map, residents=residents, broker_uid=broker_uid,
        gids_for_uid=gids_for_uid)


def assert_dir_resident_unwritable(
    directory: str,
    *,
    label: str,
    remedy: str,
    stake: str,
    uid_map: dict[int, str],
    residents: dict[str, dict],
    broker_uid: Optional[int] = None,
    gids_for_uid: Callable[[int], set[int]] = _resident_gids,
) -> str:
    """Prove a directory is unwritable by every resident, or raise ConfigError
    naming the offending path."""
    if broker_uid is None:
        broker_uid = os.geteuid()
    real = os.path.realpath(directory)

    # Resident identities.
    names = {n for n in uid_map.values() if isinstance(n, str)}
    names |= {n for n in residents if isinstance(n, str)}
    # Uids that are genuinely someone else (see CARVE-OUT above).
    other_uids = {uid for uid in uid_map if uid != broker_uid}
    resident_gids: set[int] = set()
    for uid in other_uids:
        resident_gids |= gids_for_uid(uid)

    # ---- RULE 1: never inside a resident volume ---------------------------
    home_roots = {os.path.realpath(f"/home/{n}"): f"resident home /home/{n}"
                  for n in sorted(names)}
    for name in sorted(names):
        declared = residents.get(name, {}).get("writable_roots", [])
        if isinstance(declared, list):
            for root in declared:
                if isinstance(root, str) and root:
                    home_roots[os.path.realpath(root)] = (
                        f"declared writable root of {name} ({root})")
    # path_map host targets count only when they land inside one of the roots above
    # (see the NB in the block comment: /srv/disjorn-ro is a path_map target AND the
    # intended specs dir).
    for name in sorted(names):
        pmap = residents.get(name, {}).get("path_map") or {}
        if not isinstance(pmap, dict):
            continue
        for container_prefix, host_target in pmap.items():
            if not isinstance(host_target, str) or not host_target:
                continue
            target_real = os.path.realpath(host_target)
            if any(_is_within(target_real, r) for r in list(home_roots)):
                home_roots.setdefault(
                    target_real,
                    f"path_map target of {name} ({container_prefix} -> "
                    f"{host_target}) inside a resident volume")
    for root, why in sorted(home_roots.items()):
        if _is_within(real, root):
            raise ConfigError(
                f"{label} is resident-writable: {directory!r} "
                f"resolves to {real!r}, which is inside {why}. {remedy}")

    # ---- RULE 2: not writable by any resident, leaf or parent -------------
    if not os.path.isdir(real):
        raise ConfigError(
            f"{label} does not exist or is not a directory: "
            f"{directory!r} (resolved {real!r}). Refusing to start rather than "
            f"guess — an absent directory cannot be verified unwritable.")
    for i, component in enumerate(_path_components(real)):
        is_leaf = i == 0
        try:
            st = os.stat(component)
        except OSError as exc:
            raise ConfigError(
                f"{label} path component {component!r} cannot be "
                f"stat()ed ({exc}); refusing to start (cannot verify it is "
                f"resident-unwritable)") from None
        mode = st.st_mode
        sticky = bool(mode & stat.S_ISVTX) and not is_leaf
        why = None
        if st.st_uid in other_uids and mode & stat.S_IWUSR:
            why = (f"owned by resident uid {st.st_uid} "
                   f"({uid_map.get(st.st_uid)}) and owner-writable")
        elif st.st_gid in resident_gids and mode & stat.S_IWGRP and not sticky:
            why = f"group-writable by gid {st.st_gid}, a group a resident is in"
        elif mode & stat.S_IWOTH and not sticky:
            why = "world-writable"
        if why is not None:
            raise ConfigError(
                f"{label} is resident-writable: path component "
                f"{component!r} (of {real!r}) is {why}. {stake} "
                f"Refusing to start.")
    return real


# --------------------------------------------------------------------------
# Argument validation. Every verb has an explicit schema; unknown keys are rejected;
# every value is type- and range-checked before a handler sees it.
# --------------------------------------------------------------------------

def _check_int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = args.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool):
        raise _bad(f"{key} must be an integer")
    if not lo <= v <= hi:
        raise _bad(f"{key} must be between {lo} and {hi}")
    return v


def _check_str(args: dict, key: str, *, required: bool = False,
               max_len: int = 1000) -> Optional[str]:
    v = args.get(key)
    if v is None:
        if required:
            raise _bad(f"missing required arg: {key}")
        return None
    if not isinstance(v, str):
        raise _bad(f"{key} must be a string")
    if not 1 <= len(v) <= max_len:
        raise _bad(f"{key} length must be 1..{max_len}")
    return v


def _reject_unknown(args: dict, allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise _bad(f"unknown args: {sorted(unknown)}")


def _check_date(args: dict, key: str) -> str:
    v = _check_str(args, key, required=True, max_len=10)
    assert v is not None
    if not _DATE_RE.match(v):
        raise _bad(f"{key} must be YYYY-MM-DD")
    try:
        _dt.date.fromisoformat(v)
    except ValueError as exc:
        raise _bad(f"{key}: {exc}") from None
    return v


# --------------------------------------------------------------------------
# Default file-proposal transport: post to #custodian via the Disjorn SDK as the
# broker's own bot identity. Kept behind a callable so tests stub it.
# --------------------------------------------------------------------------

def _sdk_transport(disjorn_cfg: dict, body: str) -> dict:
    """POST body to the configured custodian channel."""
    import asyncio

    from disjorn_sdk import DisjornClient  # deferred import: not needed in tests

    url = disjorn_cfg["url"]
    channel_id = int(disjorn_cfg["custodian_channel_id"])
    with open(disjorn_cfg["api_key_path"], "r", encoding="utf-8") as fh:
        api_key = fh.read().strip()

    async def _post() -> dict:
        client = DisjornClient(url, api_key=api_key)
        try:
            msg = await client.send(channel_id, body)
        finally:
            await client.aclose()
        return {"seq": msg.get("seq"), "message_id": msg.get("id")}

    return asyncio.run(_post())


# --------------------------------------------------------------------------
# Plan Room API transport (SPECS/2026-08-20-plan-room.md). Kept behind a callable so
# tests stub it, exactly like _sdk_transport above.
# --------------------------------------------------------------------------

def _api_label(path: str) -> str:
    """What to CALL the surface in a refusal."""
    return "apps API" if path.startswith("/apps") else "plan room API"


def _planroom_http(disjorn_cfg: dict, method: str, path: str,
                   payload: Optional[dict] = None) -> dict:
    """One JSON call to the Disjorn server, as the broker's own bot identity."""
    import urllib.error
    import urllib.request

    base = str(disjorn_cfg.get("url") or "").rstrip("/")
    if not base:
        raise VerbError("internal", "no [disjorn].url configured")
    try:
        with open(disjorn_cfg["api_key_path"], "r", encoding="utf-8") as fh:
            api_key = fh.read().strip()
    except (KeyError, OSError) as exc:
        raise VerbError("internal", f"broker API key unreadable: {exc}") from None

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=PLANROOM_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8")).get("detail", ""))
        except Exception:  # noqa: BLE001 — a non-JSON error body is still a refusal
            pass
        # The server's refusal is carried through verbatim.
        raise VerbError("exec-failure",
                        detail or f"{_api_label(path)} returned {exc.code}",
                        status=exc.code) from None
    except Exception as exc:  # noqa: BLE001 — network, DNS, timeout, bad JSON
        raise VerbError("exec-failure",
                        f"{_api_label(path)} unreachable: {exc}") from None


def _urlq(value: str) -> str:
    import urllib.parse
    return urllib.parse.quote(value, safe="")


_PLANROOM_MODULE = None


def _load_planroom_module():
    """`harness/planroom/planroom.py` — the derivation service."""
    global _PLANROOM_MODULE
    if _PLANROOM_MODULE is not None:
        return _PLANROOM_MODULE
    import importlib.util
    # Hand the derivation service THIS module as `brokerd`.
    sys.modules.setdefault("brokerd", sys.modules[__name__])
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "planroom", "planroom.py")
    spec = importlib.util.spec_from_file_location("disjorn_planroom", path)
    if spec is None or spec.loader is None:
        raise VerbError("internal", f"plan room module not found at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _PLANROOM_MODULE = mod
    return mod


def format_board_line(card: dict) -> str:
    """One card, one line. brief's rule, inherited: NEVER PRINT A BARE IDENTIFIER —
    every row says what the thing is and where it lives, because an item you have to
    go look up is an item that gets deferred."""
    bits = [f"[{card.get('column', '?')}]", str(card.get("slug", "?"))]
    title = card.get("title")
    if title and title != card.get("slug"):
        bits.append(f"— {title}")
    tail = []
    if card.get("tier"):
        tail.append(str(card["tier"]))
    if card.get("review_owner"):
        tail.append(f"review {card['review_owner']}")
    if card.get("builder"):
        tail.append(f"builder {card['builder']}")
    if card.get("confirm_seq"):
        tail.append(f"seq {card['confirm_seq']}")
    if card.get("comment_count"):
        tail.append(f"{card['comment_count']} comment(s)")
    for flag in card.get("flags") or []:
        tail.append(f"!{flag}")
    if (card.get("deploy") or {}).get("badge"):
        tail.append(f"deploy {card['deploy']['badge']}")
    if card.get("blocked"):
        tail.append(f"BLOCKED: {card.get('blocked_reason') or 'no reason given'}")
    line = " ".join(bits)
    return f"{line}  ·  {' · '.join(tail)}" if tail else line


def format_board_face(face: dict) -> str:
    """The board's own staleness, said out loud."""
    if face.get("available") is False:
        return f"UNAVAILABLE — {face.get('unavailable_reason', 'no reason given')}"
    head = str(face.get("mirror_head") or "?")[:12]
    badge = (face.get("deploy") or {}).get("badge", "unknown")
    out = (f"derived {face.get('derived_at', '?')} from mirror {head}; "
           f"deploy {badge}")
    for note in face.get("notes") or []:
        out += f"\nnote: {note}"
    return out


# --------------------------------------------------------------------------
# start-build (WP-L4): spec parsing, slug/branch derivation, the build-session
# prompt, and #custodian narration. Pure functions — no I/O, no broker state — so
# the confirm gate, the slug rules, and every narration shape are unit-testable in
# isolation, exactly like the argv validators above.
# --------------------------------------------------------------------------

def _clean_field(value: str) -> Optional[str]:
    """A spec field value, or None if it is blank or still the TEMPLATE.md
    placeholder (angle-bracketed `<...>`)."""
    v = value.strip()
    if not v or v in {"-", "_"}:
        return None
    if v.startswith("<") and v.endswith(">"):
        return None
    return v


def parse_spec_status(text: str) -> Optional[str]:
    """The status token under `## Status` (e.g. 'confirmed'), lowercased, or None if
    the section is absent."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == "## status":
            for follow in lines[i + 1:]:
                s = follow.strip()
                if not s or s.startswith("<!--"):
                    continue
                if s.startswith("#"):  # next heading, no value in the section
                    return None
                return s.strip("`").strip().lower()
            return None
    return None


def replace_spec_status(text: str, new_status: str, comment: str) -> Optional[str]:
    """Rewrite the `## Status` token in a spec to `new_status`, followed by ONE HTML
    comment line saying who moved it and why."""
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.strip().lower() != "## status":
            continue
        for j in range(i + 1, len(lines)):
            st = lines[j].strip()
            if not st or st.startswith("<!--"):
                continue
            if st.startswith("#"):
                return None
            lines[j] = f"{new_status}\n<!-- {comment} -->\n"
            return "".join(lines)
        return None
    return None


def _status_comment_text(text: str, cap: int = 300) -> str:
    """Make resident-influenced text safe INSIDE an HTML comment."""
    flat = " ".join(str(text).split())
    flat = re.sub(r"-{2,}", "-", flat).replace(">", "&gt;")
    return flat[:cap]


def build_outcome_class(publish: dict, unit_reason: "str | None") -> str:
    """'failed' or 'done', from the harvest lines — THE ladder, in this order:
      1. the unit itself failed (`unit_reason`)      -> failed
      2. ANY PUBLISH-FAILED line                     -> failed
      3. at least one PUBLISHED or NO-COMMITS line   -> done
      4. nothing at all                              -> failed (never assume
         success from silence).
    format_build_outcome narrates from it and spec_status_after_build stamps
    the spec from it: one ladder, so the banner and the file can never disagree
    about whether a build failed."""
    if unit_reason is not None or publish.get("failed"):
        return "failed"
    if publish.get("published") or publish.get("no_commits"):
        return "done"
    return "failed"


def spec_status_after_build(*, branch: str, publish: dict,
                            unit_reason: "str | None") -> tuple[str, str]:
    """(status token, comment) the spec should carry once its build is terminal."""
    published = publish.get("published", [])
    verdict = build_outcome_class(publish, unit_reason)
    if verdict == "failed":
        if unit_reason is not None:
            why = unit_reason
        elif publish.get("failed"):
            why = "publish failed: " + "; ".join(
                f"{repo}: {err}" for repo, err in publish["failed"])
        else:
            why = NO_HARVEST_REASON
        where = ""
        if published:
            where = " Published anyway: " + ", ".join(
                f"{repo} {sha}" for repo, sha in published) + "."
        return ("failed", f"build failed: {_status_comment_text(why)}.{where} "
                          "To allow another build, set this back to `confirmed` "
                          "(the confirm record above still stands).")
    if published:
        shas = ", ".join(f"{repo} {sha}" for repo, sha in published)
        return (f"built@{branch}",
                f"build published: {_status_comment_text(shas)} — on the branch "
                "for review, nothing merged. `board --mark-merged` advances "
                "this to `merged` once the merge lands.")
    return ("confirmed", "the build ran and produced no commits — no branch, "
                         "nothing to review; buildable again.")


def parse_confirm_record(text: str) -> dict:
    """`{confirmed_by, seq}` from the `## Confirm record` section."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "## confirm record":
            start = i + 1
            break
    out: dict = {"confirmed_by": None, "seq": None}
    if start is None:
        return out
    for line in lines[start:]:
        if line.strip().startswith("## "):
            break  # next section
        # MATCH THE WORDS, NOT THE ASTERISKS.
        plain = re.sub(r"[*_`]", "", line)
        m = re.match(r"\s*-\s*Confirmed by\s*:\s*(.*)$", plain, re.I)
        if m:
            out["confirmed_by"] = _clean_field(m.group(1))
        m = re.match(r"\s*-\s*#custodian seq\s*:\s*(.*)$", plain, re.I)
        if m:
            raw = _clean_field(m.group(1))
            if raw is not None:
                digits = re.search(r"\d+", raw)
                out["seq"] = int(digits.group()) if digits else None
    return out


_BUILD_CALLER_RE = re.compile(r"^res-([a-z][a-z0-9]{0,30})$")


def build_identity_from_caller(caller: str) -> str:
    """Short build identity ("claudette") from a uid_map caller name
    ("res-claudette")."""
    m = _BUILD_CALLER_RE.match(caller or "")
    if not m:
        raise VerbError("internal",
                        f"cannot derive a build identity from caller {caller!r} "
                        "(expected res-<name>); refusing rather than guessing")
    return m.group(1)


def slug_from_spec_filename(filename: str) -> str:
    """`SPECS/YYYY-MM-DD-<name>.md` -> `YYYY-MM-DD-<name>` (branch = loop/<slug>)."""
    base = os.path.basename(filename)
    if base.endswith(".md"):
        base = base[:-3]
    m = _SPEC_STEM_RE.match(base)
    if not m:
        raise _bad(f"spec filename does not yield a valid slug: {base!r} "
                   "(expected SPECS/YYYY-MM-DD-<kebab-name>.md)")
    try:
        _dt.date.fromisoformat(m.group(1))
    except ValueError:
        raise _bad(f"spec filename date is not a real date: {base!r}") from None
    return base


def build_session_prompt(spec_text: str, *, slug: str, branch: str) -> str:
    """The committed spec plus a one-paragraph preamble, fed to the build session on
    STDIN."""
    return (
        f"Build exactly what the spec below describes.\n"
        f"Your branch `{branch}` is already created and checked out in every "
        f"clone under `~/work`. Your worktree and the rules you work under are "
        f"in your CLAUDE.md; this message is the spec and nothing else.\n"
        f"When you are finished OR you have stopped, print the final JSON "
        f"object your CLAUDE.md describes as the last thing on stdout.\n\n"
        f"--- SPEC ({slug}) ---\n{spec_text}"
    )


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _json_object_from_text(text: str) -> "dict | None":
    """Pull a JSON object out of a chunk of model prose."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Fenced blocks: take the LAST one — the report is the closing artifact, and a
    # spec quoted earlier in the reply may itself contain a fence.
    fences = _FENCED_JSON_RE.findall(text)
    for block in reversed(fences):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    # Last resort: scan backwards for a balanced brace span.
    for start in range(len(text) - 1, -1, -1):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:end + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(data, dict) and data:
                        return data
                    break
    return None


def _parse_build_report(stdout: str) -> dict:
    """Best-effort structured report from the build session's stdout for the 'done'
    line."""
    text = stdout.strip()
    files = tests = diff = "n/a"
    data: Any = None
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    if data is None and text:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                data = None
            break
    if isinstance(data, dict):
        inner = data
        for key in ("build_report", "report", "result", "reply"):
            v = data.get(key)
            if isinstance(v, dict):
                inner = v
                break
            if isinstance(v, str):
                parsed = _json_object_from_text(v)
                if isinstance(parsed, dict):
                    inner = parsed
                    break

        def _fmt(val: Any) -> str:
            if isinstance(val, list):
                return ", ".join(str(x) for x in val) or "none"
            return str(val) if val is not None else "n/a"

        files = _fmt(inner.get("files"))
        tests = _fmt(inner.get("tests"))
        diff = _fmt(inner.get("diff"))
    elif text:
        diff = text.replace("\n", " ")[:300]
    return {"files": files, "tests": tests, "diff": diff}


def _match_publish_line(line: str) -> "tuple[str, tuple[str, ...]] | None":
    """One line of wrapper stdout -> (kind, fields), or None if it is not a
    publish-protocol line."""
    for kind, rx in _PUBLISH_LINE_RES:
        m = rx.match(line)
        if m:
            return kind, m.groups()
    return None


def _parse_publish_lines(out: str) -> dict:
    """The wrapper's harvest report, extracted from a build's stdout."""
    found: dict = {"published": [], "failed": [], "no_commits": [],
                   "quarantined": []}
    for raw in (out or "").splitlines():
        hit = _match_publish_line(raw.rstrip("\r"))
        if hit is None:
            continue
        kind, groups = hit
        if kind == "published":
            entry: Any = (groups[0], groups[1].lower())
        elif kind == "failed":
            entry = (groups[0], groups[1].strip()[:MAX_PUBLISH_ERR_CHARS])
        elif kind == "no_commits":
            entry = groups[0]
        else:
            entry = (groups[0], groups[1].strip()[:MAX_QUARANTINE_PATH_CHARS])
        bucket = found[kind]
        if entry in bucket or len(bucket) >= MAX_PUBLISH_LINES:
            continue
        bucket.append(entry)
    return found


def _strip_publish_lines(out: str) -> str:
    """The same stdout with the wrapper's protocol lines removed — what the SESSION
    printed, which is what _parse_build_report must see."""
    return "\n".join(ln for ln in (out or "").splitlines()
                     if _match_publish_line(ln.rstrip("\r")) is None)


def _publish_reported(publish: dict) -> bool:
    """Did the harvest report a VERDICT for any repo?"""
    return any(publish.get(k) for k in ("published", "failed", "no_commits"))


def _quarantine_suffix(quarantined) -> str:
    """Quarantine notices, one line each, appended to WHATEVER banner results."""
    return "".join(
        f"\nquarantined: {repo} -> {path} — unharvested work from an earlier "
        f"run, preserved not deleted" for repo, path in quarantined)


def format_build_started(*, slug: str, branch: str, confirmed_by: str,
                         seq: int, eta_sec: int) -> str:
    """The 'started' state-transition line."""
    eta_min = max(1, eta_sec // 60)
    return (f"build started | {slug} -> {branch} | "
            f"confirmed by {confirmed_by} (#custodian seq {seq}) | "
            f"ETA <= {eta_min}m (guess) | no merge, no push — lands on the branch")


def format_build_done(*, slug: str, branch: str, files: str, tests: str,
                      diff: str, tier: str = "pending", published=(),
                      no_commits=(), quarantined=(), mirror: str = "") -> str:
    """The 'done' state-transition line."""
    if published:
        outcome = "published: " + ", ".join(f"{repo} {sha}"
                                            for repo, sha in published)
        closing = "in the gatehouse for review — nothing merged"
    elif no_commits:
        outcome = ("no commits produced — nothing published, no branch exists ("
                   + ", ".join(no_commits) + ")")
        closing = "nothing to review — nothing merged"
    else:                       # unreachable via format_build_outcome
        outcome = "published: none reported"
        closing = "nothing to review — nothing merged"
    return (f"build done | {slug} -> {branch} | tier {tier} | {outcome} | "
            f"files: {files} | tests: {tests} | diff: {diff} | {closing}"
            + _quarantine_suffix(quarantined) + mirror)


# The wrapper's exit code for "this seat cannot run a test; nothing started".
PREFLIGHT_REFUSED_EXIT = 78


def format_build_refused(*, slug: str, branch: str, reason: str) -> str:
    """A build that never started, because its seat could not have run the tests the
    spec asks for."""
    detail = " ".join(reason.split())[:400]
    return (f"build refused | {slug} -> {branch} | nothing ran, no slot spent | "
            f"{detail or 'the build seat failed its dependency preflight'}")


def format_build_failed(*, slug: str, branch: str, reason: str, published=(),
                        no_commits=(), quarantined=(), mirror: str = "") -> str:
    """The 'failed' state-transition line — LOUD."""
    if published:
        where = ("published anyway: "
                 + ", ".join(f"{repo} {sha}" for repo, sha in published))
    elif no_commits:
        where = "nothing published — no commits, no branch exists"
    else:
        where = "nothing published — no branch to review"
    return (f"BUILD FAILED | {slug} -> {branch} | {reason} | {where} | "
            f"a human should look" + _quarantine_suffix(quarantined) + mirror)


# The fail-closed clause.
NO_HARVEST_REASON = (
    "the wrapper printed no publish lines — the harvest never reported "
    "(killed at the cap, or a wrapper predating the publish contract): "
    "outcome unknown, nothing was published")


def format_mirror_note(branch: str, published, error: "str | None") -> str:
    """The one line that makes a PUBLISHED banner openable (spec item 5)."""
    if not published:
        return ""
    if error:
        return ("\nmirror: NOT refreshed (" + " ".join(error.split())[:200]
                + ") — the sha above is in the gatehouse but not yet readable "
                  "in /opt/disjorn; run refresh-mirror before reviewing")
    refs = ", ".join(f"gatehouse/{repo.removesuffix('.git')}/{branch}"
                     for repo, _sha in published)
    return f"\nmirror: refreshed — read it at {refs}"


def format_spec_status_note(stamp: dict) -> str:
    """One trailing line for a build banner saying what happened to the spec's
    Status line — moved (to what, in which commit) or NOT moved (and why)."""
    if not stamp:
        return ""
    if stamp.get("ok"):
        commit = stamp.get("commit") or "?"
        return f"\nspec status: {stamp.get('status')} (commit {commit})"
    return ("\nspec status: NOT updated — "
            + " ".join(str(stamp.get("why", "")).split())[:300]
            + " — fix the Status line by hand or a resident may rebuild")


def format_build_outcome(*, slug: str, branch: str, publish: dict,
                         report: "dict | None" = None,
                         unit_reason: "str | None" = None,
                         mirror: str = "") -> str:
    """THE decision: done or failed, from the wrapper's harvest lines."""
    report = report or {"files": "n/a", "tests": "n/a", "diff": "n/a"}
    published = publish.get("published", [])
    quarantined = publish.get("quarantined", [])
    no_commits = publish.get("no_commits", [])
    failed = publish.get("failed", [])
    common = {"published": published, "no_commits": no_commits,
              "quarantined": quarantined, "mirror": mirror}
    if build_outcome_class(publish, unit_reason) == "done":
        return format_build_done(slug=slug, branch=branch, files=report["files"],
                                 tests=report["tests"], diff=report["diff"],
                                 tier="pending", **common)
    if unit_reason is not None:
        return format_build_failed(slug=slug, branch=branch, reason=unit_reason,
                                   **common)
    if failed:
        errors = "; ".join(f"{repo}: {err}" for repo, err in failed)
        return format_build_failed(slug=slug, branch=branch,
                                   reason=f"publish failed: {errors}", **common)
    return format_build_failed(slug=slug, branch=branch,
                               reason=NO_HARVEST_REASON, **common)


# --------------------------------------------------------------------------
# Wake (SPECS/2026-08-25-agentic-residents.md).
# --------------------------------------------------------------------------


def new_wake_id(now: Optional[_dt.datetime] = None,
                entropy: Optional[str] = None) -> str:
    """`wake-20260825T142310Z-9f3a1c` — sortable, greppable, collision-safe."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    stamp = now.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"wake-{stamp}-{entropy or os.urandom(3).hex()}"


def format_session_time(seconds: float) -> str:
    """`4h10m` / `50m` — a day's wake wall clock, for a human reading a refusal."""
    total = max(0, int(seconds))
    hours, minutes = divmod(total // 60, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def format_wake_refusal(*, seat: str, count: int, cap: int,
                        spent_sec: float) -> str:
    """The wall a wake past the daily cap hits."""
    return (f"daily wake cap reached for {seat}: {count}/{cap} wakes, "
            f"{format_session_time(spent_sec)} of session time today. Next "
            f"wake is tomorrow (UTC), or a witnessed edit to "
            f"[wake].daily_wake_cap in broker.toml.")


def parse_review_owner(text: str) -> Optional[str]:
    """The `- **Review owner**: …` bullet's value, or None if the spec has no such
    bullet."""
    for line in text.splitlines():
        plain = re.sub(r"[*_`]", "", line)
        m = re.match(r"\s*-\s*Review owner\s*:\s*(.*)$", plain, re.I)
        if m:
            return _clean_field(m.group(1))
    return None


def review_owner_seat(raw: Optional[str],
                      known_seats: "set[str] | frozenset[str]") -> Optional[str]:
    """The SEAT a review-owner line names (`Claudette` -> `res-claudette`), or None
    when it names nobody this house runs as."""
    if not raw:
        return None
    m = re.match(r"[\s*_`]*([A-Za-z][A-Za-z0-9_-]{0,30})", raw)
    if not m:
        return None
    seat = f"res-{m.group(1).lower()}"
    return seat if seat in known_seats else None


# --------------------------------------------------------------------------
# Bot-to-bot summon hops (SPECS/2026-08-24-custodian-mention-summons.md).
# --------------------------------------------------------------------------

DEFAULT_HOP_CAP = 8
DEFAULT_DAILY_HOP_CAP = 24


def format_hop_refusal(*, work_item: str, count: int, cap: int,
                       daily: bool = False) -> str:
    """The refusal line, fixed format."""
    if daily:
        return (f"summon refused: {work_item} at {count}/{cap} bot hops today "
                f"— the daily ceiling, which clears at 00:00 UTC")
    return (f"summon refused: {work_item} at {count}/{cap} bot hops "
            f"— parked until a human posts on it")


class HopLedger:
    """The per-work-item hop counter, persisted and restart-proof."""

    def __init__(self, path: str, *, hop_cap: int = DEFAULT_HOP_CAP,
                 daily_hop_cap: int = DEFAULT_DAILY_HOP_CAP,
                 today_fn: Optional[Callable[[], str]] = None) -> None:
        self.path = path
        self.hop_cap = hop_cap
        self.daily_hop_cap = daily_hop_cap
        self._today_fn = today_fn or (
            lambda: _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))
        self._lock = threading.Lock()

    # ------------------------------------------------------------- state io

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict) -> None:
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _record(self, state: dict, work_item: str) -> dict:
        rec = state.get(work_item)
        if not isinstance(rec, dict):
            rec = {}
        today = self._today_fn()
        if rec.get("day") != today:
            rec["day"] = today
            rec["day_hops"] = 0
        rec.setdefault("hops", 0)
        rec.setdefault("unpark_seq", 0)
        state[work_item] = rec
        return rec

    # -------------------------------------------------------------- public

    def spend(self, work_item: str) -> dict:
        """Charge one hop."""
        with self._lock:
            state = self._load()
            rec = self._record(state, work_item)
            hops, day_hops = int(rec["hops"]), int(rec["day_hops"])
            if day_hops >= self.daily_hop_cap:
                return {"allowed": False, "reason": "daily-ceiling",
                        "count": day_hops, "cap": self.daily_hop_cap,
                        "refusal": format_hop_refusal(
                            work_item=work_item, count=day_hops,
                            cap=self.daily_hop_cap, daily=True)}
            if hops >= self.hop_cap:
                return {"allowed": False, "reason": "parked",
                        "count": hops, "cap": self.hop_cap,
                        "refusal": format_hop_refusal(
                            work_item=work_item, count=hops, cap=self.hop_cap)}
            rec["hops"] = hops + 1
            rec["day_hops"] = day_hops + 1
            self._save(state)
            return {"allowed": True, "reason": "hop", "count": rec["hops"],
                    "cap": self.hop_cap, "day_count": rec["day_hops"],
                    "day_cap": self.daily_hop_cap}

    def unpark(self, work_item: str, seq: Optional[int] = None) -> dict:
        """A human posted on this work item: the chain resumes at 0/cap."""
        with self._lock:
            state = self._load()
            rec = self._record(state, work_item)
            if seq is not None and seq <= int(rec["unpark_seq"]):
                return {"reset": False, "count": int(rec["hops"]),
                        "cap": self.hop_cap}
            rec["hops"] = 0
            if seq is not None:
                rec["unpark_seq"] = int(seq)
            self._save(state)
            return {"reset": True, "count": 0, "cap": self.hop_cap,
                    "day_count": int(rec["day_hops"]),
                    "day_cap": self.daily_hop_cap}


# --------------------------------------------------------------------------
# The broker.
# --------------------------------------------------------------------------

class Broker:
    """Unix-socket verb broker. Construct with parsed broker.toml + a path to
    verbs.toml (re-read per request — that's the kill-switch property)."""

    # How often an ADOPTED build's unit is polled for its terminal state.
    BUILD_POLL_SEC = 5.0

    def __init__(
        self,
        config: dict,
        verbs_path: str,
        *,
        transport: Optional[Callable[[dict, str], dict]] = None,
        build_spawn: Optional[Callable[[list[str]], Any]] = None,
        planroom_api: Optional[Callable[..., dict]] = None,
        apps_spawn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = config
        self.verbs_path = verbs_path
        self.transport = transport or _sdk_transport
        # How the board verbs reach the Disjorn server's /planroom surface.
        self.planroom_api = planroom_api or _planroom_http
        # How a detached build session is launched.
        self._build_spawn = build_spawn or self._default_build_spawn
        # How one apps-build TURN is launched.
        self._apps_spawn = apps_spawn or self._default_apps_spawn
        broker_cfg = config.get("broker", {})
        self.socket_path: str = broker_cfg.get("socket_path", DEFAULT_SOCKET_PATH)
        self.audit_path: str = broker_cfg["audit_log"]
        # uid map: TOML keys are strings; normalise to int -> resident name.
        self.uid_map: dict[int, str] = {
            int(uid): name for uid, name in config.get("uids", {}).items()
        }
        self.residents: dict[str, dict] = config.get("residents", {})
        self.commands: dict[str, Any] = config.get("commands", {})
        self.paths: dict[str, str] = config.get("paths", {})
        self.disjorn: dict[str, Any] = config.get("disjorn", {})
        # APPS v1 stage 2 (SPECS/2026-09-06-apps-builder-seat.md §B).
        apps_cfg = config.get("apps")
        self.apps_configured: bool = isinstance(apps_cfg, dict) and bool(apps_cfg)
        self.apps: dict[str, Any] = {
            **APPS_DEFAULTS, **(apps_cfg if isinstance(apps_cfg, dict) else {})}
        # Session -> turn currently in flight, and the lock that makes claiming one
        # atomic.
        self._apps_lock = threading.Lock()
        # app_id -> (session, turn): one turn at a time PER APP, not per session
        # (BLOCK).
        self._active_apps: dict[str, tuple[int, int]] = {}
        self._apps_threads: list[threading.Thread] = []
        # Why the verb is off, in one flat sentence, or None.
        self._apps_disabled_reason: Optional[str] = None
        self.planroom: dict[str, Any] = (
            config.get("planroom", {})
            if isinstance(config.get("planroom"), dict) else {})
        self._planroom_lock = threading.Lock()
        self._planroom_thread: Optional[threading.Thread] = None
        # Daily per-resident action budget (WP-H12).
        self.budgets: dict[str, Any] = config.get("budgets", {})
        # start-build (WP-L4) config: the detached build-session launch contract
        # (command + session_argv + model pin), the SPECS/ dir the confirm gate
        # reads, the wall-clock cap, and the per-day build budget.
        self.start_build: dict[str, Any] = config.get("start_build", {})
        # The bot-to-bot hop wall.
        self.summon_hops: dict[str, Any] = config.get("summon_hops", {}) or {}
        self.hops: Optional[HopLedger] = None
        if self.summon_hops:
            state_path = self.summon_hops.get("state_path")
            if not isinstance(state_path, str) or not state_path:
                raise ConfigError(
                    "[summon_hops] is configured but summon_hops.state_path is "
                    "missing; refusing to start (a hop counter that cannot "
                    "persist unparks every parked chain on restart)")
            self.hops = HopLedger(
                state_path,
                hop_cap=int(self.summon_hops.get("hop_cap", DEFAULT_HOP_CAP)),
                daily_hop_cap=int(self.summon_hops.get(
                    "daily_hop_cap", DEFAULT_DAILY_HOP_CAP)))
        # BR-1: the build identity is derived from the CALLER —
        # build_identity_from_caller — and [start_build].resident is dead.
        if "resident" in self.start_build:
            print("disjorn-broker: WARNING [start_build].resident is IGNORED "
                  "since BR-1 (2026-08-14): builds run as the resident that "
                  "CALLS start-build (SO_PEERCRED), never as a configured "
                  "name. Delete the line from broker.toml.", file=sys.stderr)
        # BL-D1: the confirm gate's REAL authorization is that specs_dir is
        # resident-unwritable.
        self.specs_dir_real: Optional[str] = None
        if self.start_build and self._spec_repo() is None:
            print("disjorn-broker: WARNING [start_build].spec_repo is not set: "
                  "the broker cannot move a spec's Status line to `building` / "
                  "`built@<branch>` / `failed` as its build moves, so a spec "
                  "under construction keeps reading `confirmed` and the board "
                  "lists it as buildable. Set spec_repo to the canonical repo "
                  "the mirror follows (e.g. /home/plink/Disjorn/Disjorn).",
                  file=sys.stderr)
        if self.start_build:
            specs_dir = self.start_build.get("specs_dir")
            if not isinstance(specs_dir, str) or not specs_dir:
                raise ConfigError(
                    "[start_build] is configured but start_build.specs_dir is "
                    "missing; refusing to start (the confirm gate has no "
                    "trustworthy source)")
            self.specs_dir_real = assert_specs_dir_resident_unwritable(
                specs_dir, uid_map=self.uid_map, residents=self.residents)
        self.wake: dict[str, Any] = config.get("wake", {}) or {}
        self.wake_callers: frozenset[str] = frozenset()
        self.wake_seats: frozenset[str] = frozenset()
        self.wake_spool_real: Optional[str] = None
        self.seat_names: frozenset[str] = frozenset(
            n for n in ({v for v in self.uid_map.values() if isinstance(v, str)}
                        | {n for n in self.residents if isinstance(n, str)})
            if _BUILD_CALLER_RE.match(n))
        if self.wake:
            self.wake_callers = self._parse_wake_callers()
            self.wake_seats = self._parse_wake_seats()
            spool = self.wake.get("spool_dir")
            if not isinstance(spool, str) or not spool:
                raise ConfigError(
                    "[wake] is configured but wake.spool_dir is missing; "
                    "refusing to start (a wake with nowhere to land is a wake "
                    "the seat never hears about)")
            self.wake_spool_real = assert_dir_resident_unwritable(
                spool,
                label="wake.spool_dir",
                remedy=("Keep the spool somewhere plink owns and no resident "
                        "mounts (e.g. /var/lib/disjorn-broker/wake-spool); the "
                        "seat's runner only ever READS it."),
                stake=("A resident that can write the spool can write itself a "
                       "wake, and nothing self-wakes."),
                uid_map=self.uid_map, residents=self.residents)
        self._audit_lock = threading.Lock()
        # Build-budget lock (H13-D4): count-with-reservation is held under this, so
        # two concurrent start-builds can NEVER both slip past the cap — the
        # check-then-act race the red-team flagged is closed here.
        self._build_lock = threading.Lock()
        # Wake-budget lock: the day's count is read from the spool and the new
        # record is written under this one lock, so two wakes pressed at once cannot
        # both read the same pre-cap count.
        self._wake_lock = threading.Lock()
        # Action-budget lock (H13-D4, extended to EVERY numeric budget): same
        # count-with-reservation discipline as builds.
        self._action_lock = threading.Lock()
        # Per-resident build reservations for the day: resident -> (utc_date,
        # count).
        self._builds: dict[str, tuple[Optional[str], int]] = {}
        # Same shape for the action budget: resident -> (utc_date, count).
        self._actions: dict[str, tuple[Optional[str], int]] = {}
        # BL-D4: slugs of builds currently in flight.
        self._active_builds: set[str] = set()
        # Detached build reaper threads, kept ONLY so tests can join them;
        # production never waits on a build — detachment is the whole point.
        self._build_threads: list[threading.Thread] = []
        self._listener: Optional[socket.socket] = None
        self._closed = False

        # The verb table.
        self.verbs: dict[str, Callable[[str, dict], tuple]] = {
            "restart-disjorn": self._verb_restart_disjorn,
            "run-server-tests": self._verb_run_server_tests,
            "refresh-mirror": self._verb_refresh_mirror,
            "start-build": self._verb_start_build,
            "classify-diff": self._verb_classify_diff,
            "read-prod-logs": self._verb_read_prod_logs,
            "read-own-log": self._verb_read_own_log,
            "read-metrics": self._verb_read_metrics,
            "file-proposal": self._verb_file_proposal,
            "query-own-audit": self._verb_query_own_audit,
            "summon-hop": self._verb_summon_hop,
            # The one verb no seat may call: dispatch refuses it to every resident
            # identity before verbs.toml is even read, and refuses every other verb
            # to a wake caller.
            WAKE_VERB: self._verb_wake,
            # Plan Room (SPECS/2026-08-20-plan-room.md).
            "board-list": self._verb_board_list,
            "board-card": self._verb_board_card,
            "board-search": self._verb_board_search,
            "board-flag": self._verb_board_flag,
            "board-comment": self._verb_board_comment,
            # APPS v1 stage 2.
            "apps-build": self._verb_apps_build,
        }

        # The seat map is checked against the server's `bots` table ONCE, here,
        # while there is still a human watching the boot.
        if self.apps_configured:
            self._apps_disabled_reason = self._apps_seat_map_failure()
            if self._apps_disabled_reason:
                self._audit("broker", "apps-build", {}, False,
                            self._apps_disabled_reason)

    # -------------------------------------------------------- wake config

    def _parse_wake_callers(self) -> frozenset[str]:
        """`[wake].callers` — the identities that may wake a seat."""
        callers = self.wake.get("callers")
        if not isinstance(callers, list) or not callers or not all(
                isinstance(c, str) and c for c in callers):
            raise ConfigError(
                "[wake] is configured but wake.callers is missing or is not a "
                "non-empty list of identity names; refusing to start")
        mapped = {v for v in self.uid_map.values() if isinstance(v, str)}
        for caller in callers:
            if _BUILD_CALLER_RE.match(caller):
                raise ConfigError(
                    f"wake.callers names the resident seat {caller!r}: a seat "
                    "may never wake anyone (nothing self-wakes). Refusing to "
                    "start.")
            if caller not in mapped:
                raise ConfigError(
                    f"wake.callers names {caller!r}, which has no uid in "
                    "[uids]: it could never be authenticated at the socket, so "
                    "the wake surface would read as armed while refusing every "
                    "call. Add the uid or drop the name.")
        return frozenset(callers)

    def _parse_wake_seats(self) -> frozenset[str]:
        """`[wake].residents` — the seats that may BE woken."""
        seats = self.wake.get("residents")
        if not isinstance(seats, list) or not seats or not all(
                isinstance(s, str) and s for s in seats):
            raise ConfigError(
                "[wake] is configured but wake.residents is missing or is not "
                "a non-empty list of seat names; refusing to start")
        for seat in seats:
            if seat not in self.seat_names:
                raise ConfigError(
                    f"wake.residents names {seat!r}, which is not a resident "
                    "of this house ([uids] / [residents]); refusing to start")
        return frozenset(seats)

    def _wake_session_cap(self) -> int:
        cap = self.wake.get("session_cap_sec", DEFAULT_WAKE_SESSION_CAP_SEC)
        return cap if isinstance(cap, int) and cap > 0 else DEFAULT_WAKE_SESSION_CAP_SEC

    def _wake_grace(self) -> int:
        grace = self.wake.get("grace_sec", DEFAULT_WAKE_GRACE_SEC)
        return grace if isinstance(grace, int) and grace >= 0 else DEFAULT_WAKE_GRACE_SEC

    # ------------------------------------------------------------- audit

    def _audit(self, resident: str, verb: str, args: Any, allowed: bool,
               result_summary: str, extra: Optional[dict] = None) -> None:
        """One JSON line per call."""
        rec = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "resident": resident,
            "verb": verb,
            "args": args,
            "allowed": allowed,
            "result_summary": result_summary[:500],
        }
        for key, value in (extra or {}).items():
            rec.setdefault(str(key), value)
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with self._audit_lock:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # -------------------------------------------------------------- budget

    def _daily_action_cap(self, resident: str) -> Optional[int]:
        """Per-resident daily action cap from `[budgets]`, or None (off)."""
        per = self.budgets.get(resident)
        if isinstance(per, dict) and isinstance(per.get("daily_action_cap"), int):
            return per["daily_action_cap"]
        default = self.budgets.get("default_daily_action_cap")
        return default if isinstance(default, int) else None

    def _count_today_allowed(self, resident: str) -> int:
        """How many ALLOWED actions this resident has today (UTC), read from the
        audit log — the same source the metrics producer aggregates, so the count is
        authoritative and restart-proof."""
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        n = 0
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if resident not in raw:  # safe prefilter: name is in the JSON
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (rec.get("resident") == resident and rec.get("allowed") is True
                            and str(rec.get("ts", ""))[:10] == today):
                        n += 1
        except OSError:
            return 0
        return n

    def _reserve_action(self, resident: str, cap: int) -> None:
        """Race-safe action-budget check + reservation (H13-D4)."""
        with self._action_lock:
            today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
            date, count = self._actions.get(resident, (None, 0))
            if date != today:  # first action this UTC day (or after restart)
                count = self._count_today_allowed(resident)
            if count >= cap:
                self._actions[resident] = (today, count)
                raise VerbError("over-budget",
                                f"daily action budget of {cap} reached for {resident}")
            self._actions[resident] = (today, count + 1)

    def _release_action(self, resident: str) -> None:
        """Refund an action reservation when the call turned out to be a DENIAL
        (bad-args / over-budget): denials are audited allowed=False and must not
        consume budget — a resident cannot exhaust its own cap by being refused (the
        WP-H12 contract, preserved verbatim under reservation)."""
        with self._action_lock:
            date, count = self._actions.get(resident, (None, 0))
            if count > 0:
                self._actions[resident] = (date, count - 1)

    # -------------------------------------------------------- build budget

    def _daily_build_cap(self, resident: str) -> Optional[int]:
        """Per-day build cap for a resident."""
        per = self.start_build.get("per_resident")
        if isinstance(per, dict):
            r = per.get(resident)
            if isinstance(r, dict) and isinstance(r.get("daily_build_cap"), int):
                return r["daily_build_cap"]
        cap = self.start_build.get("daily_build_cap", DEFAULT_DAILY_BUILD_CAP)
        return cap if isinstance(cap, int) else DEFAULT_DAILY_BUILD_CAP

    def _count_builds_today(self, resident: str, today: str) -> int:
        """Builds this resident GENUINELY STARTED today (UTC)."""
        n = 0
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if "start-build" not in raw or resident not in raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (rec.get("resident") == resident
                            and rec.get("verb") == "start-build"
                            and rec.get("allowed") is True
                            and rec.get("build_started") is True
                            and str(rec.get("ts", ""))[:10] == today):
                        n += 1
        except OSError:
            return 0
        return n

    def _reserve_build(self, resident: str, slug: str) -> tuple[int, Optional[int]]:
        """Race-safe build-budget check + reservation (H13-D4:
        count-with-reservation under a lock, NEVER check-then-act on the audit
        file), plus the BL-D4 in-flight uniqueness claim on the slug."""
        cap = self._daily_build_cap(resident)
        with self._build_lock:
            if slug in self._active_builds:
                # bad-args (a denial, so it burns no budget and audits
                # allowed=False): the caller can fix it by waiting or by writing a
                # distinct spec.
                raise _bad(f"a build for {slug} is already running "
                           f"(branch loop/{slug}); wait for it to finish")
            today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
            date, count = self._builds.get(resident, (None, 0))
            if date != today:  # first build this UTC day (or after restart)
                count = self._count_builds_today(resident, today)
            if cap is not None and count >= cap:
                self._builds[resident] = (today, count)
                raise VerbError("over-budget",
                                f"daily build budget of {cap} reached for {resident}")
            self._builds[resident] = (today, count + 1)
            self._active_builds.add(slug)
            return count + 1, cap

    def _release_build(self, resident: str, slug: str) -> None:
        """Refund a reservation AND drop the slug claim when the launch itself never
        started (a build that ran and then failed keeps its slot — it burned the
        attempt; see _finish_build)."""
        with self._build_lock:
            date, count = self._builds.get(resident, (None, 0))
            if count > 0:
                self._builds[resident] = (date, count - 1)
            self._active_builds.discard(slug)

    def _finish_build(self, slug: str) -> None:
        """Release the BL-D4 slug claim when a started build reaches a terminal
        state."""
        with self._build_lock:
            self._active_builds.discard(slug)

    def join_builds(self, timeout: float = 5.0) -> None:
        """Join detached build reaper threads — TEST convenience only."""
        for t in list(self._build_threads):
            t.join(timeout)

    # --------------------------------------------------------------- core

    def dispatch(self, uid: int, verb: Any, args: Any) -> dict:
        """Authorize + execute one request."""
        resident = self.uid_map.get(uid)
        caller = resident if resident is not None else f"uid:{uid}"

        if not isinstance(verb, str) or not isinstance(args, dict):
            self._audit(caller, str(verb)[:100], args, False, "denied: malformed request")
            return self._err("bad-args", "request must be {verb: str, args: object}")

        if resident is None:
            self._audit(caller, verb, args, False, "denied: unknown caller uid")
            return self._err("unknown-caller", f"uid {uid} is not a configured resident")

        if verb not in self.verbs:
            self._audit(caller, verb, args, False, "denied: unknown verb")
            return self._err("unknown-verb", f"no such verb: {verb}")

        # Wake identity, BEFORE the kill switch and before any handler: the wake
        # verb is the one verb whose caller is not a seat, and the two halves of
        # that are enforced here rather than left to verbs.toml.
        refusal = self._check_wake_identity(resident, verb)
        if refusal is not None:
            self._audit(caller, verb, args, False, f"denied: {refusal}")
            return self._err("verb-disabled", refusal)

        # Kill switch: fresh read of verbs.toml on every request; missing file,
        # missing resident section or missing key all mean OFF (fail closed).
        try:
            with open(self.verbs_path, "rb") as fh:
                verbs_cfg = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            self._audit(caller, verb, args, False, "denied: verbs.toml unreadable")
            return self._err("internal", "verb configuration unavailable")
        if verbs_cfg.get(resident, {}).get(verb, False) is not True:
            self._audit(caller, verb, args, False, "denied: verb disabled for resident")
            return self._err("verb-disabled", f"{verb} is not enabled for {resident}")

        # Daily per-resident action budget (WP-H12).
        cap = self._daily_action_cap(resident)
        reserved = False
        if cap is not None:
            try:
                self._reserve_action(resident, cap)
            except VerbError as exc:
                self._audit(caller, verb, args, False,
                            f"denied: over daily action budget ({cap})")
                return self._err(exc.code, exc.message)
            reserved = True

        try:
            out = self.verbs[verb](resident, args)
            # Verbs return (result, summary) or (result, summary, audit_extra); only
            # start-build uses the third slot today (BL-D3's `build_started`
            # marker), so no other handler had to change.
            result, summary, extra = out if len(out) == 3 else (*out, None)
        except VerbError as exc:
            allowed = exc.code not in ("bad-args", "over-budget", "apps-refused")
            if reserved and not allowed:
                self._release_action(resident)
            self._audit(caller, verb, args, allowed,
                        f"{'error' if allowed else 'denied'}: {exc.message}")
            return self._err(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 — never crash the daemon on a verb
            self._audit(caller, verb, args, True, f"error: internal: {exc!r}")
            return self._err("internal", "internal broker error")

        self._audit(caller, verb, args, True, summary, extra=extra)
        return {"ok": True, "verb": verb, "result": result}

    @staticmethod
    def _err(code: str, message: str) -> dict:
        return {"ok": False, "error": {"code": code, "message": message}}

    # ---------------------------------------------------------- subprocess

    def _argv(self, key: str, default: list[str]) -> list[str]:
        argv = self.commands.get(key, default)
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise VerbError("internal", f"commands.{key} must be a list of strings")
        return list(argv)

    def _run(self, argv: list[str], timeout: int,
             cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        # Fixed argv list, shell NEVER involved.
        try:
            return subprocess.run(  # noqa: S603 — argv list, no shell
                argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            raise VerbError("exec-failure", f"command timed out after {timeout}s") from None
        except OSError as exc:
            raise VerbError("exec-failure", f"command failed to start: {exc}") from None

    # -------------------------------------------------------------- verbs
    # Each returns (result_dict, audit_summary).

    def _verb_restart_disjorn(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, set())
        # `sudo -n`: never prompts; works only because of the single sudoers line
        # installed by harness/keyboard/04-broker.sh.
        argv = self._argv("restart_disjorn",
                          ["sudo", "-n", "systemctl", "restart", "disjorn"])
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["restart-disjorn"])
        out = (cp.stdout + cp.stderr).strip()[-2000:]
        return ({"exit_code": cp.returncode, "output": out},
                f"exit={cp.returncode}")

    def _verb_run_server_tests(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, set())
        argv = self._argv("run_server_tests", [
            "/home/plink/Disjorn/Disjorn/server/.venv/bin/python",
            "-m", "pytest", "tests", "-q",
        ])
        cwd = self.commands.get("run_server_tests_cwd",
                                "/home/plink/Disjorn/Disjorn/server")
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["run-server-tests"], cwd=cwd)
        lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
        summary = lines[-1] if lines else "(no output)"
        return ({"exit_code": cp.returncode, "summary": summary},
                f"exit={cp.returncode}: {summary}"[:300])

    # ------------------------------------------------- the gatehouse fetch
    # SPECS/2026-08-14-file-vision.md item 1. `refresh-mirror` used to move `main`
    # and nothing else, so the mirror could tell a resident what production runs and
    # could not show them a single branch anyone was being asked to review. Every
    # branch now lands under refs/gatehouse/<repo>/*.
    _GATEHOUSE_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def _gatehouse_fetch_argvs(self) -> list[tuple[str, list[str]]]:
        """One fixed argv per entitled gatehouse repo."""
        base_dir = self.commands.get("refresh_mirror_gatehouse_dir")
        repos = self.commands.get("refresh_mirror_gatehouse_repos")
        if not base_dir or not repos:
            return []                     # not configured = today's behaviour
        if not isinstance(base_dir, str) or not isinstance(repos, list):
            raise VerbError("internal",
                            "commands.refresh_mirror_gatehouse_dir must be a "
                            "string and _repos a list of strings")
        fetch = self._argv("refresh_mirror_gatehouse_fetch",
                           ["git", "-C", "/srv/disjorn-ro", "fetch", "--prune"])
        out: list[tuple[str, list[str]]] = []
        for repo in repos:
            if not isinstance(repo, str) or not self._GATEHOUSE_REPO_RE.match(repo):
                raise VerbError("internal",
                                f"commands.refresh_mirror_gatehouse_repos holds "
                                f"{repo!r}, which is not a plain repo name")
            out.append((repo, [*fetch, f"{base_dir}/{repo}.git",
                               f"+refs/heads/*:refs/gatehouse/{repo}/*"]))
        return out

    def _gatehouse_count_argv(self, repo: str) -> list[str]:
        """Fixed argv listing the refs the mirror HOLDS for `repo`."""
        if not isinstance(repo, str) or not self._GATEHOUSE_REPO_RE.match(repo):
            raise VerbError("internal",
                            f"gatehouse repo {repo!r} is not a plain repo name")
        base = self._argv("refresh_mirror_gatehouse_count",
                          ["git", "-C", "/srv/disjorn-ro", "for-each-ref",
                           "--format=%(refname)"])
        return [*base, f"refs/gatehouse/{repo}/"]

    def _gatehouse_present(self, repo: str, timeout: int) -> Optional[int]:
        """How many refs the mirror holds for `repo` — an INVENTORY, next to
        `arrived`'s DELTA."""
        try:
            cp = self._run(self._gatehouse_count_argv(repo), timeout)
        except VerbError:
            return None
        if cp.returncode != 0:
            return None
        return sum(1 for line in cp.stdout.splitlines() if line.strip())

    @staticmethod
    def _parse_fetch_refs(output: str) -> tuple[list[str], list[str]]:
        """(arrived, vanished) ref names out of `git fetch --prune` chatter."""
        arrived, vanished = [], []
        for line in output.splitlines():
            ref = line.strip().rsplit(" ", 1)[-1].strip()
            if not ref:
                continue
            if "[deleted]" in line:
                vanished.append(ref)
            elif "[new branch]" in line or "[new ref]" in line:
                arrived.append(ref)
        return arrived, vanished

    def _fetch_gatehouse_into_mirror(self, timeout: int) -> list[dict]:
        """Run every gatehouse fetch; return one record per repo."""
        records = []
        for repo, argv in self._gatehouse_fetch_argvs():
            cp = self._run(argv, timeout)
            if cp.returncode != 0:
                raise VerbError(
                    "exec-failure",
                    f"gatehouse fetch for {repo} exit {cp.returncode}: "
                    f"{(cp.stderr or cp.stdout).strip()[:500]}")
            arrived, vanished = self._parse_fetch_refs(cp.stderr + cp.stdout)
            records.append({"repo": repo, "arrived": arrived,
                            "vanished": vanished,
                            "present": self._gatehouse_present(repo, timeout)})
        return records

    def _verb_refresh_mirror(self, resident: str, args: dict) -> tuple[dict, str]:
        """Fast-forward the shared read-only repo mirror to the canonical repo's
        main, THEN re-fetch every entitled gatehouse repo's branches into
        refs/gatehouse/<repo>/*."""
        _reject_unknown(args, set())
        timeout = SUBPROCESS_TIMEOUTS["refresh-mirror"]
        head_argv = self._argv("refresh_mirror_head", [
            "git", "-C", "/srv/disjorn-ro", "rev-parse", "--short", "HEAD"])

        def _head() -> str:
            cp = self._run(head_argv, timeout)
            if cp.returncode != 0:
                raise VerbError("exec-failure",
                                f"rev-parse exit {cp.returncode}: "
                                f"{cp.stderr.strip()[:300]}")
            return cp.stdout.strip()

        before = _head()
        self._ff_mirror_main(timeout)
        gatehouse = self._fetch_gatehouse_into_mirror(timeout)
        # The mirror has just moved, so every card derived from it may have moved
        # with it.
        planroom = ({"rebuilt": False, "reason": "disabled by config"}
                    if not self.planroom.get("rebuild_on_refresh", True)
                    else self._planroom_rebuild("refresh-mirror"))
        head = _head()
        summary = f"mirror at {head}" + ("" if head == before
                                         else f" (was {before})")
        # No news stays no line.
        empty = ", ".join(rec["repo"] for rec in gatehouse
                          if rec["present"] == 0)
        if empty:
            summary = f"{summary}; gatehouse EMPTY for {empty}"
        moved = "; ".join(
            f"{rec['repo']}: +{len(rec['arrived'])} new, "
            f"-{len(rec['vanished'])} harvested or deleted"
            for rec in gatehouse if rec["arrived"] or rec["vanished"])
        if moved:
            summary = f"{summary}; gatehouse {moved}"
        if planroom.get("transitions"):
            summary = f"{summary}; plan room {planroom['transitions']} move(s)"
        elif planroom.get("rebuilt") is False and planroom.get("reason") \
                not in ("no [planroom].index configured", "disabled by config"):
            summary = f"{summary}; PLAN ROOM REBUILD FAILED: {planroom['reason']}"
        return ({"head": head, "before": before, "updated": head != before,
                 "gatehouse": gatehouse, "planroom": planroom}, summary[:300])

    def _ff_mirror_main(self, timeout: int) -> None:
        """Fetch origin into the read-only mirror and fast-forward it to origin/main
        — the two fixed argvs `refresh-mirror` has always run, factored so the
        spec-status stamp can use the SAME refresh (never a second implementation of
        "the mirror is fresh")."""
        for key, default in (
            ("refresh_mirror_fetch",
             ["git", "-C", "/srv/disjorn-ro", "fetch", "origin"]),
            ("refresh_mirror_update",
             ["git", "-C", "/srv/disjorn-ro", "merge", "--ff-only", "origin/main"]),
        ):
            cp = self._run(self._argv(key, default), timeout)
            if cp.returncode != 0:
                raise VerbError("exec-failure",
                                f"{key} exit {cp.returncode}: "
                                f"{(cp.stderr or cp.stdout).strip()[:500]}")

    # ------------------------------------------------- spec Status stamping

    def _spec_repo(self) -> Optional[tuple[str, str, str]]:
        """(repo path, branch, SPECS subdir) of the CANONICAL repo whose SPECS/ the
        mirror follows, from `[start_build].spec_repo` (+ `spec_repo_branch`,
        default main; `spec_repo_subdir`, default SPECS)."""
        repo = self.start_build.get("spec_repo")
        if not isinstance(repo, str) or not repo:
            return None
        branch = self.start_build.get("spec_repo_branch", "main")
        subdir = self.start_build.get("spec_repo_subdir", "SPECS")
        if (not isinstance(branch, str) or not branch
                or not isinstance(subdir, str) or not subdir):
            return None
        return repo, branch, subdir.strip("/")

    def _git(self, repo: str, *args: str, stdin: Optional[str] = None,
             env: Optional[dict] = None) -> subprocess.CompletedProcess:
        """One git command against the canonical repo, fixed argv, no shell."""
        argv = [*self._argv("spec_repo_git", ["git"]), "-C", repo, *args]
        full_env = None
        if env:
            full_env = dict(os.environ)
            full_env.update(env)
        try:
            return subprocess.run(  # noqa: S603 — argv list, no shell
                argv, capture_output=True, text=True, input=stdin,
                timeout=SUBPROCESS_TIMEOUTS["spec-status"], env=full_env)
        except subprocess.TimeoutExpired:
            raise VerbError("exec-failure", "git timed out") from None
        except OSError as exc:
            raise VerbError("exec-failure", f"git failed to start: {exc}") from None

    def _git_ok(self, repo: str, *args: str, **kw) -> str:
        cp = self._git(repo, *args, **kw)
        if cp.returncode != 0:
            raise VerbError("exec-failure",
                            f"git {args[0]} exit {cp.returncode}: "
                            f"{(cp.stderr or cp.stdout).strip()[:300]}")
        return cp.stdout

    # -- the local coverage record ------------------------------------------
    # THE PUSH LOG'S SIBLING (spec, confirmed). A stamp commit is made with git
    # plumbing straight onto the canonical repo's branch: no push, so it never meets
    # the pre-receive hook, so it can never have a push-log line.

    LOCAL_LOG_NAME = "disjorn-local-log"
    LOCAL_STAMP = "local-stamp"

    def _local_coverage_log(self) -> Optional[str]:
        """Where the record goes: beside the push log, `[gate].local_log`,
        defaulting to <[gate].canonical_repo>/hooks/disjorn-local-log."""
        gate = self.config.get("gate")
        if not isinstance(gate, dict):
            return None
        path = gate.get("local_log")
        if isinstance(path, str) and path:
            return path
        canonical = gate.get("canonical_repo")
        if isinstance(canonical, str) and canonical:
            return os.path.join(canonical, "hooks", self.LOCAL_LOG_NAME)
        return None

    def _record_local_commit(self, sha: str,
                             outcome: str = LOCAL_STAMP) -> str:
        """Append `LOCAL <ts> <sha> <outcome>`."""
        path = self._local_coverage_log()
        if not path:
            return ("no [gate].local_log or [gate].canonical_repo is "
                    f"configured, so no coverage record names {sha[:7]}; the "
                    "digest will have to guess what put it on the branch")
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, f"LOCAL {ts} {sha} {outcome}\n".encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            return (f"coverage record NOT written to {path}: {exc}; the next "
                    f"digest will report {sha[:7]} as unexplained")
        return ""

    def _stamp_spec_status(self, slug: str, new_status: str, comment: str, *,
                           expect: tuple[str, ...]) -> dict:
        """Move a spec's `## Status` line in the CANONICAL repo and commit it, then
        fast-forward the read-only mirror so residents (and this broker's own
        confirm gate) read the new word at once."""
        cfg = self._spec_repo()
        if cfg is None:
            return {"ok": False, "status": new_status, "commit": None,
                    "why": "start_build.spec_repo is not configured, so the "
                           "broker cannot move Status lines"}
        repo, branch, subdir = cfg
        relpath = f"{subdir}/{slug}.md"
        ref = f"refs/heads/{branch}"
        try:
            old_sha = self._git_ok(repo, "rev-parse", "--verify", "--quiet",
                                   ref).strip()
            text = self._git_ok(repo, "show", f"{old_sha}:{relpath}")
            have = parse_spec_status(text)
            if have not in expect:
                return {"ok": False, "status": new_status, "commit": None,
                        "why": f"{relpath} on {branch} says {have!r}, expected "
                               f"one of {sorted(expect)} — left as is"}
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
            new_text = replace_spec_status(
                text, new_status,
                f"set by the broker on {stamp} (start-build, {slug}): {comment}")
            if new_text is None:
                return {"ok": False, "status": new_status, "commit": None,
                        "why": f"{relpath} has no parseable ## Status line"}
            blob = self._git_ok(repo, "hash-object", "-w", "--stdin",
                                stdin=new_text).strip()
            fd, index = tempfile.mkstemp(prefix="disjorn-broker-index.")
            os.close(fd)
            os.unlink(index)          # git wants a path, not an empty file
            env = {"GIT_INDEX_FILE": index}
            try:
                self._git_ok(repo, "read-tree", old_sha, env=env)
                self._git_ok(repo, "update-index", "--add", "--cacheinfo",
                             f"100644,{blob},{relpath}", env=env)
                tree = self._git_ok(repo, "write-tree", env=env).strip()
            finally:
                try:
                    os.unlink(index)
                except OSError:
                    pass
            msg = (f"{slug}: Status -> {new_status}\n\nStamped by the broker "
                   f"(start-build). {comment}\n")
            ident = {"GIT_AUTHOR_NAME": "disjorn-broker",
                     "GIT_AUTHOR_EMAIL": "broker@disjorn.local",
                     "GIT_COMMITTER_NAME": "disjorn-broker",
                     "GIT_COMMITTER_EMAIL": "broker@disjorn.local"}
            commit = self._git_ok(repo, "commit-tree", tree, "-p", old_sha,
                                  "-m", msg, env=ident).strip()
            self._git_ok(repo, "update-ref", "-m", f"broker: {slug} -> {new_status}",
                         ref, commit, old_sha)
        except VerbError as exc:
            return {"ok": False, "status": new_status, "commit": None,
                    "why": exc.message}
        except Exception as exc:  # noqa: BLE001 — a stamp must never sink a build
            return {"ok": False, "status": new_status, "commit": None,
                    "why": repr(exc)}
        result = {"ok": True, "status": new_status, "commit": commit[:7],
                  "why": ""}
        notes: list[str] = []
        note = self._record_local_commit(commit)
        if note:
            notes.append(note)
        # Courtesy sync of the keyboard's worktree, only when it is provably safe:
        # HEAD is this branch and the file has no local edits.
        try:
            head = self._git(repo, "symbolic-ref", "--quiet", "HEAD").stdout.strip()
            if head == ref:
                # "Clean" = worktree AND index still equal the commit we just moved
                # past (old_sha), not HEAD — HEAD is already the new commit, against
                # which an untouched checkout looks modified.
                dirty = (self._git(repo, "diff", "--quiet", old_sha, "--",
                                   relpath).returncode != 0
                         or self._git(repo, "diff", "--quiet", "--cached",
                                      old_sha, "--", relpath).returncode != 0)
                if dirty:
                    notes.append(f"{relpath} has local edits in the working "
                                 "tree; the commit landed on the branch but "
                                 "the worktree was not touched")
                else:
                    self._git_ok(repo, "checkout", "HEAD", "--", relpath)
        except Exception as exc:  # noqa: BLE001 — courtesy only
            notes.append(f"worktree not synced: {exc!r}")
        # Carry the word to the mirror the gate and the residents read.
        try:
            self._ff_mirror_main(SUBPROCESS_TIMEOUTS["refresh-mirror"])
        except VerbError as exc:
            notes.append(f"mirror NOT refreshed: {exc.message}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"mirror NOT refreshed: {exc!r}")
        result["why"] = "; ".join(notes)
        return result

    # ------------------------------------------------------------ start-build

    def _specs_dir(self) -> str:
        """The SPECS/ dir the confirm gate reads."""
        if self.specs_dir_real:
            return self.specs_dir_real
        d = self.start_build.get("specs_dir")
        if not d or not isinstance(d, str):
            raise VerbError("internal", "start_build.specs_dir is not configured")
        return d

    def _resolve_spec_path(self, spec: str) -> str:
        """Map caller input to a real spec file, CONFINED to the configured SPECS/
        dir. realpath() resolves BOTH `..` traversal and symlink escape, then we
        require the resolved file to sit DIRECTLY in SPECS/ (the flat
        one-file-per-spec layout) and end in .md."""
        if spec.startswith("-") or "\x00" in spec:
            raise _bad("spec must not start with '-' or contain NUL")
        specs_dir = self._specs_dir()
        candidate = spec if os.path.isabs(spec) else os.path.join(specs_dir, spec)
        real = os.path.realpath(candidate)
        real_specs = os.path.realpath(specs_dir)
        if os.path.dirname(real) != real_specs or not real.endswith(".md"):
            raise _bad("spec must be a .md file directly inside the SPECS/ directory")
        if not os.path.isfile(real):
            raise _bad("spec file does not exist")
        return real

    def _read_confirmed_spec(self, path: str) -> dict:
        """Read + validate the spec at `path`: status must be 'confirmed' and the
        confirm record must be filled (Confirmed by + #custodian seq)."""
        try:
            if os.path.getsize(path) > MAX_SPEC_BYTES:
                raise _bad(f"spec exceeds {MAX_SPEC_BYTES} bytes")
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise VerbError("exec-failure", f"spec not readable: {exc}") from None

        status = parse_spec_status(text)
        if status != "confirmed":
            raise _bad(f"spec status is {status!r}, not 'confirmed' — no build "
                       "starts without a confirmed spec")
        confirm = parse_confirm_record(text)
        if not confirm.get("confirmed_by") or confirm.get("seq") is None:
            raise _bad("spec has no confirm record (need 'Confirmed by' + "
                       "'#custodian seq') — the confirm record is the instance "
                       "selector the broker verifies mechanically")
        slug = slug_from_spec_filename(path)
        return {"text": text, "slug": slug, "branch": f"loop/{slug}",
                "confirmed_by": confirm["confirmed_by"], "seq": confirm["seq"]}

    def _build_argv(self, slug: str, build_resident: str) -> list[str]:
        """The detached build command — a PURE function of config + the validated
        slug."""
        command = self.start_build.get("command", [])
        if (not isinstance(command, list) or not command
                or not all(isinstance(a, str) for a in command)):
            raise VerbError("internal",
                            "start_build.command must be a non-empty list of strings")
        # BR-1: the identity is the CALLER's, passed in — see
        # build_identity_from_caller. [start_build].resident is dead config and
        # warned about at startup.
        resident_arg = build_resident
        session_argv = self.start_build.get("session_argv", [])
        if (not isinstance(session_argv, list)
                or not all(isinstance(a, str) for a in session_argv)):
            raise VerbError("internal",
                            "start_build.session_argv must be a list of strings")
        model = self.start_build.get("model")
        if not isinstance(model, str) or not model.strip():
            raise VerbError("internal",
                            "start_build.model must be a non-empty string "
                            "(WP-L5 pin; no fallback)")
        return [*command, resident_arg, slug, *session_argv, "--model", model.strip()]

    def _default_build_spawn(self, argv: list[str], *, stdout: Any,
                             stderr: Any) -> subprocess.Popen:
        """Launch the build DETACHED so it outlives this request."""
        return subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    # -------------------------------------------------- build output (BL-D2)

    def _build_log_dir(self) -> str:
        """Where the detached build's stdout/stderr files live."""
        d = self.config.get("broker", {}).get("build_log_dir")
        if isinstance(d, str) and d:
            return d
        base = os.path.dirname(self.audit_path)
        if base:
            candidate = os.path.join(base, "build-logs")
            try:
                os.makedirs(candidate, mode=0o700, exist_ok=True)
                return candidate
            except OSError:
                pass
        return tempfile.gettempdir()

    def _open_build_logs(self, slug: str) -> tuple[str, str, Any, Any]:
        """Create the two 0600 output files for one build and return (out_path,
        err_path, out_fh, err_fh). mkstemp() creates them with mode 0600 and O_EXCL,
        so no other local user can read a build's output and nothing can be
        pre-planted at the path."""
        d = self._build_log_dir()
        try:
            out_fd, out_path = tempfile.mkstemp(
                prefix=f"disjorn-build-{slug}.", suffix=".out", dir=d)
            try:
                err_fd, err_path = tempfile.mkstemp(
                    prefix=f"disjorn-build-{slug}.", suffix=".err", dir=d)
            except OSError:
                os.close(out_fd)
                os.unlink(out_path)
                raise
        except OSError as exc:
            raise VerbError("exec-failure",
                            f"cannot create build output file: {exc}") from None
        return out_path, err_path, os.fdopen(out_fd, "wb"), os.fdopen(err_fd, "wb")

    @staticmethod
    def _close_build_logs(*handles: Any) -> None:
        for fh in handles:
            try:
                fh.close()
            except Exception:  # noqa: BLE001 — closing twice is fine
                pass

    @staticmethod
    def _unlink_build_logs(*paths: str) -> None:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _read_build_tail(path: str, limit: int = MAX_BUILD_LOG_TAIL) -> str:
        """The last `limit` bytes of a build output file, decoded leniently."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit))
                data = fh.read(limit)
        except OSError:
            return ""
        return data.decode("utf-8", "replace")

    @staticmethod
    def _read_build_head(path: str, limit: int = MAX_BUILD_LOG_TAIL) -> str:
        """The FIRST `limit` bytes, truncated at the last complete line."""
        try:
            with open(path, "rb") as fh:
                data = fh.read(limit + 1)
        except OSError:
            return ""
        if len(data) <= limit:
            return data.decode("utf-8", "replace")
        text = data[:limit].decode("utf-8", "replace")
        return text[:text.rfind("\n") + 1]

    def _harvest_report(self, out_path: str, out_tail: str) -> dict:
        """The wrapper's publish lines for one build: parsed from the log's head AND
        tail, because the two ends carry different halves of the protocol
        (quarantine at provisioning time, verdicts after the container exits)."""
        return _parse_publish_lines(self._read_build_head(out_path) + "\n"
                                    + out_tail)

    # ------------------------------------------- transient-unit lifecycle (L4)

    def _start_build_argv(self, key: str, default: list[str]) -> list[str]:
        """A fixed argv list out of `[start_build]`, validated like `_argv`
        validates `[commands]`."""
        argv = self.start_build.get(key, default)
        if not isinstance(argv, list) or not argv or not all(
                isinstance(a, str) for a in argv):
            raise VerbError("internal",
                            f"start_build.{key} must be a non-empty list of strings")
        return list(argv)

    def _build_unit_state(self, slug: str) -> str:
        """systemd's word for what the build's unit is doing — `active`, `failed`,
        `inactive`, or `unknown` if we cannot ask."""
        try:
            argv = self._start_build_argv(
                "unit_state_command",
                ["systemctl", "show", "--property=ActiveState", "--value"])
            cp = self._run([*argv, build_unit_name(slug)], 30)
        except Exception:  # noqa: BLE001 — a state probe never breaks a reaper
            return "unknown"
        if cp.returncode != 0:
            return "unknown"
        return (cp.stdout or "").strip().lower() or "unknown"

    def _stop_build_unit(self, slug: str, build_resident: str) -> bool:
        """Ask systemd to stop a build's unit."""
        try:
            argv = self._start_build_argv(
                "stop_command",
                ["sudo", "-n", "/usr/local/lib/disjorn/disjorn-build-launch", "stop"])
            cp = self._run([*argv, build_resident, slug], 60)
            return cp.returncode == 0
        except Exception:  # noqa: BLE001 — never crash a reaper on cleanup
            return False

    def _sidecar_path(self, slug: str) -> str:
        return os.path.join(self._build_log_dir(), f"{slug}{BUILD_SIDECAR_SUFFIX}")

    def _write_build_sidecar(self, meta: dict, *, out_path: str, err_path: str,
                             timeout: int) -> str:
        """Persist everything a FUTURE broker process needs to finish this build's
        story: which unit, which branch, which spool files, and when the cap
        expires."""
        path = self._sidecar_path(meta["slug"])
        record = {
            "schema": BUILD_SIDECAR_SCHEMA,
            "slug": meta["slug"],
            "branch": meta["branch"],
            "unit": build_unit_name(meta["slug"]),
            # Since BR-1 these two agree by construction — `build_resident` is
            # DERIVED from `caller` (strip res-, launch helper re-derives uid/
            # home/config from it).
            "caller": meta.get("resident"),
            "build_resident": meta.get("build_resident", ""),
            "confirmed_by": meta.get("confirmed_by"),
            "seq": meta.get("seq"),
            "out_path": out_path,
            "err_path": err_path,
            # NO pid, deliberately.
            "timeout_sec": timeout,
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "deadline": time.time() + timeout,
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        return path

    def _remove_build_sidecar(self, slug: str) -> None:
        try:
            os.unlink(self._sidecar_path(slug))
        except OSError:
            pass

    def _narrate(self, body: str) -> None:
        """Post a build state-transition line to #custodian via the broker's OWN bot
        identity — the same transport file-proposal uses."""
        try:
            self.transport(self.disjorn, body)
        except Exception:  # noqa: BLE001 — narration is legibility, not control
            pass

    def _refresh_mirror_for_banner(self, branch: str, published) -> str:
        """SPEC ITEM 5 — refresh the mirror BEFORE the banner that names a sha."""
        if not published:
            return ""
        error = None
        try:
            if not self._gatehouse_fetch_argvs():
                # No gatehouse configured: there is no mirror claim to make, so the
                # banner makes none.
                return ""
            self._fetch_gatehouse_into_mirror(
                SUBPROCESS_TIMEOUTS["refresh-mirror"])
        except VerbError as exc:
            error = exc.message
        except Exception as exc:  # noqa: BLE001 — never crash a reaper
            error = repr(exc)
        return format_mirror_note(branch, published, error)

    def _narrate_build_outcome(self, **kwargs) -> None:
        """Every terminal build banner goes through here, so the mirror fetch cannot
        be forgotten on one of the five paths that post one."""
        publish = kwargs.get("publish") or {}
        kwargs["mirror"] = self._refresh_mirror_for_banner(
            kwargs.get("branch", ""), publish.get("published", []))
        status, comment = spec_status_after_build(
            branch=kwargs.get("branch", ""), publish=publish,
            unit_reason=kwargs.get("unit_reason"))
        stamp = self._stamp_spec_status(kwargs.get("slug", ""), status, comment,
                                        expect=("building",))
        self._narrate(format_build_outcome(**kwargs)
                      + format_spec_status_note(stamp))
        # A build's terminal banner is the loudest column transition the house has —
        # `building` -> Review, or `building` -> back to Ready on a failure — and
        # the mirror was just refreshed two lines above.
        self._planroom_rebuild("build-outcome")

    def _reap_build(self, proc: Any, spec_bytes: bytes, meta: dict,
                    timeout: int, out_path: str, err_path: str) -> None:
        """Detached-build lifecycle END (runs in a daemon thread; the request
        returned long ago)."""
        slug, branch = meta["slug"], meta["branch"]
        try:
            try:
                proc.communicate(spec_bytes, timeout=timeout)
            except subprocess.TimeoutExpired:
                stopped = self._stop_build_unit(
                    slug, meta.get("build_resident", ""))
                try:
                    proc.kill()
                    proc.communicate()
                except Exception:  # noqa: BLE001 — already reaping
                    pass
                self._narrate_build_outcome(
                    slug=slug, branch=branch,
                    publish=self._harvest_report(
                        out_path, self._read_build_tail(out_path)),
                    unit_reason=f"timed out after {timeout}s — killed"
                                + ("" if stopped else
                                   " (unit stop reported a problem; check "
                                   f"systemctl status {build_unit_name(slug)})"))
                return
            except Exception as exc:  # noqa: BLE001 — broken pipe etc. = a failure
                self._narrate_build_outcome(
                    slug=slug, branch=branch,
                    publish=self._harvest_report(
                        out_path, self._read_build_tail(out_path)),
                    unit_reason=f"build error: {exc!r}")
                return

            out_s = self._read_build_tail(out_path)
            err_s = self._read_build_tail(err_path)
            # The wrapper's harvest lines are the evidence; the session's JSON
            # report is stripped out of their way and demoted to enrichment (08-13
            # spec item 3).
            publish = self._harvest_report(out_path, out_s)
            session_out = _strip_publish_lines(out_s)
            report = _parse_build_report(session_out)
            rc = getattr(proc, "returncode", None)

            # PREFLIGHT REFUSAL (exit 78, EX_CONFIG).
            if rc == PREFLIGHT_REFUSED_EXIT:
                resident = meta.get("resident")
                if resident:
                    self._release_build(resident, slug)
                # Nothing ran, the slot came back — the word comes back too.
                stamp = self._stamp_spec_status(
                    slug, "confirmed",
                    "the build seat failed its dependency preflight; nothing "
                    "ran, no slot spent, buildable again.",
                    expect=("building",))
                self._narrate(format_build_refused(
                    slug=slug, branch=branch,
                    reason=(err_s or session_out).strip()[:400])
                    + format_spec_status_note(stamp))
                return

            unit_reason = None
            if rc is not None and rc != 0:
                unit_reason = f"exit {rc}: {(err_s or session_out).strip()[:400]}"
            self._narrate_build_outcome(
                slug=slug, branch=branch, publish=publish, report=report,
                unit_reason=unit_reason)
        finally:
            self._unlink_build_logs(out_path, err_path)
            self._remove_build_sidecar(slug)
            self._finish_build(slug)

    # ------------------------------------------- reattachment after a restart

    def adopt_inflight_builds(self) -> list[str]:
        """Re-adopt builds that outlived the previous broker process, and sweep what
        did not survive."""
        adopted: list[str] = []
        keep: set[str] = set()
        try:
            log_dir = self._build_log_dir()
            entries = sorted(os.listdir(log_dir))
        except OSError:
            return adopted
        for name in entries:
            if not name.endswith(BUILD_SIDECAR_SUFFIX):
                continue
            path = os.path.join(log_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                slug = rec["slug"]
                build_unit_name(slug)          # re-validate: hostile until proven
                # The ticket must be named after the build it claims, or the slug
                # inside decides which files get deleted while the filename decides
                # nothing — a mismatch is not a build record.
                if name != f"{slug}{BUILD_SIDECAR_SUFFIX}":
                    raise ValueError("sidecar name does not match its slug")
            except Exception:  # noqa: BLE001 — an unreadable ticket is garbage
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            out_path = str(rec.get("out_path") or "")
            err_path = str(rec.get("err_path") or "")
            with self._build_lock:
                ours = slug in self._active_builds
            if ours:
                # A build THIS process already owns: its own reaper will finish the
                # story.
                keep.update({os.path.basename(p) for p in (out_path, err_path) if p})
                keep.add(name)
                continue
            state = self._build_unit_state(slug)
            if state in BUILD_ACTIVE_STATES:
                with self._build_lock:
                    self._active_builds.add(slug)
                keep.update({os.path.basename(p) for p in (out_path, err_path) if p})
                keep.add(name)
                adopted.append(slug)
                t = threading.Thread(target=self._reap_adopted_build,
                                     args=(rec,), daemon=True)
                self._build_threads.append(t)
                t.start()
            else:
                self._narrate_adopted_outcome(rec, state)
                self._unlink_build_logs(out_path, err_path)
                self._remove_build_sidecar(slug)
        # Janitor: spool files with no live ticket are orphans from a broker that
        # died mid-build.
        for name in entries:
            if name in keep or not name.startswith(BUILD_UNIT_PREFIX):
                continue
            if name.endswith(".out") or name.endswith(".err"):
                self._unlink_build_logs(os.path.join(log_dir, name))
        return adopted

    def _reap_adopted_build(self, rec: dict) -> None:
        """Watch an adopted build to its terminal state, then narrate + tidy."""
        slug = rec["slug"]
        try:
            deadline = float(rec.get("deadline") or 0.0)
            while not self._closed:
                state = self._build_unit_state(slug)
                if state not in BUILD_ACTIVE_STATES:
                    self._narrate_adopted_outcome(rec, state)
                    break
                if deadline and time.time() > deadline:
                    self._stop_build_unit(
                        slug, str(rec.get("build_resident") or ""))
                    out_path = str(rec.get("out_path") or "")
                    self._narrate_build_outcome(
                        slug=slug, branch=rec.get("branch", f"loop/{slug}"),
                        publish=self._harvest_report(
                            out_path, self._read_build_tail(out_path)),
                        unit_reason=f"timed out after {rec.get('timeout_sec')}s "
                                    "— killed (build re-adopted after a broker "
                                    "restart)")
                    break
                time.sleep(self.BUILD_POLL_SEC)
            else:
                return           # shutting down: leave the ticket for next time
        except Exception:        # noqa: BLE001 — same rule: keep the ticket
            return
        self._unlink_build_logs(str(rec.get("out_path") or ""),
                                str(rec.get("err_path") or ""))
        self._remove_build_sidecar(slug)
        self._finish_build(slug)

    def _narrate_adopted_outcome(self, rec: dict, state: str) -> None:
        """The done/failed line for a build this process did not launch."""
        slug = rec["slug"]
        branch = rec.get("branch", f"loop/{slug}")
        out_path = str(rec.get("out_path") or "")
        out_s = self._read_build_tail(out_path)
        err_s = self._read_build_tail(str(rec.get("err_path") or ""))
        publish = self._harvest_report(out_path, out_s)
        session_out = _strip_publish_lines(out_s)
        report = _parse_build_report(session_out)
        note = (err_s or session_out).strip()[:400]
        unit_reason = None
        if state == "failed":
            unit_reason = (f"re-adopted after a broker restart; the unit ended in "
                           f"state {state} — outcome unknown"
                           + (f": {note}" if note else ""))
        elif not _publish_reported(publish):
            unit_reason = ("re-adopted after a broker restart and the wrapper "
                           f"printed no publish lines (unit state {state}) — "
                           "the harvest never reported: outcome unknown, nothing "
                           "was published" + (f": {note}" if note else ""))
        self._narrate_build_outcome(
            slug=slug, branch=branch, publish=publish, report=report,
            unit_reason=unit_reason)

    def _verb_start_build(self, resident: str, args: dict) -> tuple[dict, str]:
        """Launch a DETACHED build of a CONFIRMED spec to `loop/<slug>` (WP-L4)."""
        _reject_unknown(args, {"spec"})
        spec_arg = _check_str(args, "spec", required=True, max_len=300)
        assert spec_arg is not None
        spec_path = self._resolve_spec_path(spec_arg)
        meta = self._read_confirmed_spec(spec_path)

        wake = self._active_wake(resident)
        if wake is not None:
            self._assert_woken_build_allowed(resident, wake, meta["text"])

        # Build the argv (pure config + validated slug) BEFORE reserving, so a
        # misconfiguration refuses without burning a budget slot.
        build_resident = build_identity_from_caller(resident)
        meta["build_resident"] = build_resident
        argv = self._build_argv(meta["slug"], build_resident)
        timeout = int(self.start_build.get("timeout_sec", START_BUILD_DEFAULT_TIMEOUT))
        prompt = build_session_prompt(
            meta["text"], slug=meta["slug"], branch=meta["branch"])

        # Reserve the budget slot + claim the slug under the lock (H13-D4, BL-D4).
        used, cap = self._reserve_build(resident, meta["slug"])

        # BL-D2: the build's stdout/stderr land in 0600 temp FILES, never in pipes
        # this privileged process must drain.
        try:
            out_path, err_path, out_fh, err_fh = self._open_build_logs(meta["slug"])
        except BaseException:
            self._release_build(resident, meta["slug"])
            raise

        # The claim ticket for the transient unit, written BEFORE the launch so a
        # broker that dies mid-spawn still leaves a trail for the next process to
        # adopt (adopt_inflight_builds).
        meta["resident"] = resident
        try:
            self._write_build_sidecar(meta, out_path=out_path, err_path=err_path,
                                      timeout=timeout)
        except OSError as exc:
            self._release_build(resident, meta["slug"])
            self._close_build_logs(out_fh, err_fh)
            self._unlink_build_logs(out_path, err_path)
            raise VerbError("exec-failure",
                            f"cannot record the build: {exc}") from None

        stamp = self._stamp_spec_status(
            meta["slug"], "building",
            f"build running as {build_unit_name(meta['slug'])} -> {meta['branch']}, "
            f"launched by {build_resident} (confirmed by {meta['confirmed_by']}, "
            f"#custodian seq {meta['seq']}). Not buildable again until this "
            "line moves.",
            expect=("confirmed",))

        # 'started' — a state transition; best-effort (a failed post must never sink
        # a launched build, and is never a heartbeat).
        self._narrate(format_build_started(
            slug=meta["slug"], branch=meta["branch"],
            confirmed_by=meta["confirmed_by"], seq=meta["seq"], eta_sec=timeout)
            + format_spec_status_note(stamp))

        try:
            proc = self._build_spawn(argv, stdout=out_fh, stderr=err_fh)
        except OSError as exc:
            # Never spawned: refund the slot, drop the slug claim, delete the
            # (empty) output files.
            self._release_build(resident, meta["slug"])
            self._close_build_logs(out_fh, err_fh)
            self._unlink_build_logs(out_path, err_path)
            self._remove_build_sidecar(meta["slug"])
            unstamp = self._stamp_spec_status(
                meta["slug"], "confirmed",
                f"the launch failed before anything ran ({_status_comment_text(exc)}); "
                "no build happened, buildable again.",
                expect=("building",)) if stamp.get("ok") else {}
            self._narrate(format_build_failed(
                slug=meta["slug"], branch=meta["branch"],
                reason=f"launch failed: {exc}") + format_spec_status_note(unstamp))
            raise VerbError("exec-failure",
                            f"build failed to launch: {exc}") from None
        finally:
            # The child holds its own dups of these fds; the broker must not.
            self._close_build_logs(out_fh, err_fh)

        t = threading.Thread(
            target=self._reap_build,
            args=(proc, prompt.encode("utf-8"), meta, timeout, out_path, err_path),
            daemon=True)
        self._build_threads.append(t)
        t.start()

        result = {"started": True, "branch": meta["branch"], "slug": meta["slug"],
                  "pid": getattr(proc, "pid", None),
                  # The transient unit the build runs in.
                  "unit": build_unit_name(meta["slug"]),
                  "confirmed_by": meta["confirmed_by"], "seq": meta["seq"],
                  # What happened to the spec's Status line (-> `building`).
                  # ok=False is NOT a refusal — the build runs regardless — but the
                  # caller should say so where a human will read it.
                  "spec_status": stamp}
        budget_str = f"{used}/{cap}" if cap is not None else str(used)
        # Third element = audit extras.
        return (result,
                f"build {meta['slug']} -> {meta['branch']} launched "
                f"(budget {budget_str})",
                {"build_started": True})

    # ------------------------------------------------------- apps-build (§E)
    # THE SHAPE OF THIS VERB, and why it is not start-build with different strings.
    # A spec build is one long-running child whose stdout IS the evidence; an app
    # build turn is a unit run by ANOTHER seat, whose evidence the broker can only
    # read off the filesystem.

    def _apps_message_db(self) -> Optional[str]:
        """The server DB the seat-map boot check reads, resolved the way
        metrics.py's `gate_paths` resolves it: `[gate].message_db`, else
        <[gate].deploy_tree>/server/data/disjorn.db."""
        gate = self.config.get("gate")
        if not isinstance(gate, dict):
            return None
        path = gate.get("message_db")
        if isinstance(path, str) and path:
            return path
        deploy_tree = gate.get("deploy_tree")
        if isinstance(deploy_tree, str) and deploy_tree:
            return os.path.join(deploy_tree, "server", "data", "disjorn.db")
        return None

    def _apps_seat_map_failure(self) -> Optional[str]:
        """None if `[apps].seat_bots` agrees with the server's `bots` table, else
        one flat sentence saying how it does not (§B)."""
        seat_bots = self.apps.get("seat_bots")
        if not isinstance(seat_bots, dict) or not seat_bots:
            return ("[apps].seat_bots is empty, so no seat maps to a builder "
                    "bot and no handoff could ever be attributed")
        for seat, bot_id in seat_bots.items():
            if not isinstance(seat, str) or not APPS_SEAT_RE.match(seat):
                return (f"[apps].seat_bots names {seat!r}, which is not a "
                        "res-<name> resident seat")
            if not isinstance(bot_id, int) or isinstance(bot_id, bool):
                return (f"[apps].seat_bots maps {seat} to {bot_id!r}, which is "
                        "not a bot id")
        db_path = self._apps_message_db()
        if not db_path or not os.path.exists(db_path):
            return ("the server database named by [gate].message_db / "
                    "[gate].deploy_tree is not readable, so the seat map "
                    "cannot be checked against the bots that exist")
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            return (f"the server database at {db_path} could not be opened "
                    f"read-only ({exc}), so the seat map cannot be checked")
        try:
            for seat, bot_id in seat_bots.items():
                row = db.execute("select name from bots where id = ?",
                                 (int(bot_id),)).fetchone()
                if row is None:
                    return (f"[apps].seat_bots maps {seat} to bot {bot_id}, "
                            "which does not exist on this server")
                expected = seat[len("res-"):].lower()
                if str(row[0] or "").lower() != expected:
                    return (f"[apps].seat_bots maps {seat} to bot {bot_id}, "
                            f"whose name is {row[0]!r} and not {expected!r}")
        except sqlite3.Error as exc:
            return (f"the server's bots table could not be read ({exc}), so "
                    "the seat map cannot be checked")
        finally:
            db.close()
        return None

    def _apps_unavailable(self) -> None:
        """Raise the one refusal that means "this broker cannot run turns"."""
        if not self.apps_configured:
            raise VerbError("apps-refused",
                            "apps-build is not configured on this broker")
        if self._apps_disabled_reason:
            raise VerbError("apps-refused",
                            "apps-build is disabled: the seat map failed its "
                            f"boot check — {self._apps_disabled_reason}")

    # -- config readers ---------------------------------------------------

    def _apps_int(self, key: str) -> int:
        value = self.apps.get(key, APPS_DEFAULTS.get(key))
        if not isinstance(value, int) or isinstance(value, bool):
            return int(APPS_DEFAULTS.get(key, 0))
        return value

    def _apps_num(self, key: str) -> float:
        """A duration knob."""
        value = self.apps.get(key, APPS_DEFAULTS.get(key))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return float(APPS_DEFAULTS.get(key, 0))
        return float(value)

    def _apps_argv(self, key: str) -> list[str]:
        """A fixed argv list out of `[apps]`, validated like `[commands]` is."""
        argv = self.apps.get(key, APPS_DEFAULTS[key])
        if not isinstance(argv, list) or not argv or not all(
                isinstance(a, str) for a in argv):
            raise VerbError("internal",
                            f"apps.{key} must be a non-empty list of strings")
        return list(argv)

    def _apps_log_dir(self) -> str:
        """Where a turn's launcher spool and its sidecar live: plink-owned, 0700,
        resident-unreachable."""
        d = self.apps.get("log_dir") or APPS_DEFAULTS["log_dir"]
        os.makedirs(d, mode=0o700, exist_ok=True)
        return str(d)

    def _apps_turn_dir(self, session: int, turn: int) -> str:
        return os.path.join(str(self.apps.get("turns_root")
                                or APPS_DEFAULTS["turns_root"]),
                            str(session), str(turn))

    def _apps_sidecar_path(self, session: int, turn: int) -> str:
        return os.path.join(self._apps_log_dir(),
                            f"{session}-{turn}{APPS_SIDECAR_SUFFIX}")

    # -- launch -----------------------------------------------------------

    def _default_apps_spawn(self, argv: list[str], *, stdout: Any,
                            stderr: Any) -> subprocess.Popen:
        """Launch one turn DETACHED."""
        return subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    def _apps_unit_state(self, unit: str) -> str:
        """systemd's word for an adopted turn's unit — `active`, `failed`,
        `inactive`, or `unknown` if we cannot ask."""
        try:
            cp = self._run([*self._apps_argv("unit_state_command"), unit], 30)
        except Exception:  # noqa: BLE001 — a state probe never breaks a reaper
            return "unknown"
        if cp.returncode != 0:
            return "unknown"
        return (cp.stdout or "").strip().lower() or "unknown"

    def _apps_stop_requested(self, session: int) -> bool:
        """Has the owner asked for the running turn to stop? Read off the server's
        harness-view, best-effort: a read that fails answers "not yet" and the next
        poll asks again, because a reaper must not die over a question it can
        repeat."""
        try:
            view = self._apps_harness_view(session)
        except Exception:  # noqa: BLE001 — see docstring
            return False
        return bool(view.get("stop_requested_at"))

    def _apps_send_stop(self, rec: dict) -> bool:
        """`disjorn-apps-launch stop <caller> <session> <turn>` through sudo."""
        session, turn = int(rec["session"]), int(rec["turn"])
        caller = str(rec.get("caller") or "")
        if not APPS_SEAT_RE.match(caller):
            self._audit("broker", "apps-build",
                        {"session": session, "turn": turn}, True,
                        f"stop not sent: ticket names no seat ({caller!r})")
            return False
        argv = [*self._apps_argv("stop_command"), caller, str(session), str(turn)]
        try:
            cp = self._run(argv, 30)
        except VerbError as exc:
            self._audit("broker", "apps-build",
                        {"session": session, "turn": turn}, True,
                        f"stop failed to run: {exc.message}")
            return False
        sent = cp.returncode != APPS_LAUNCH_REFUSED
        tail = (cp.stderr or cp.stdout or "").strip()[-300:]
        self._audit("broker", "apps-build",
                    {"session": session, "turn": turn}, True,
                    (f"stop sent to {rec.get('unit')}: exit {cp.returncode}"
                     if sent else
                     f"stop refused by the launcher for {rec.get('unit')}: "
                     f"exit {cp.returncode}, will ask again")
                    + (f" — {tail}" if tail and cp.returncode != 0 else ""))
        return sent

    # -- the claim --------------------------------------------------------

    def _apps_claim(self, app_id: str, session: int, turn: int) -> None:
        """§E check 4: one turn at a time per APP."""
        with self._apps_lock:
            if app_id in self._active_apps:
                raise VerbError("apps-refused",
                                "a turn is already running for this app")
            self._active_apps[app_id] = (session, turn)

    def _apps_release(self, app_id: str) -> None:
        with self._apps_lock:
            self._active_apps.pop(app_id, None)

    # -- talking to the server --------------------------------------------

    def _apps_harness_view(self, session: int) -> dict:
        """The session as the SERVER knows it (§1.1)."""
        try:
            view = self.planroom_api(
                self.disjorn, "GET", f"/apps/sessions/{session}/harness-view")
        except VerbError as exc:
            if exc.status == 404:
                raise VerbError("apps-refused", "no such build session") from None
            raise
        if not isinstance(view, dict):
            raise VerbError("exec-failure",
                            "the apps harness view returned no session")
        return view

    def _apps_post_stage(self, session: int, stage: str, detail: dict) -> bool:
        """Publish one stage event."""
        payload = {"stage": stage, "detail": apps_fit_detail(detail)}
        path = f"/apps/sessions/{session}/stage"
        for attempt in (1, 2):
            try:
                self.planroom_api(self.disjorn, "POST", path, payload)
                return True
            except VerbError as exc:
                self._audit("broker", "apps-build",
                            {"session": session, "stage": stage}, True,
                            f"stage post failed: {exc.message}")
                if exc.status == 410 or attempt == 2:
                    return False
                time.sleep(self._apps_num("poll_sec"))
            except Exception as exc:  # noqa: BLE001 — never crash a reaper
                self._audit("broker", "apps-build",
                            {"session": session, "stage": stage}, True,
                            f"stage post failed: {exc!r}")
                return False
        return False

    # -- the ledger (§E) ---------------------------------------------------

    def _apps_ledger(self, record: dict) -> None:
        """One JSON line per turn, append-only."""
        path = str(self.apps.get("ledger_path") or APPS_DEFAULTS["ledger_path"])
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._audit("broker", "apps-build",
                        {"session": record.get("session")}, True,
                        f"apps ledger unwritable: {exc}")

    def _apps_ledger_tokens_after(self, session: int) -> int:
        """The highest `tokens_after` this house has LOGGED for a session."""
        path = str(self.apps.get("ledger_path") or APPS_DEFAULTS["ledger_path"])
        best = 0
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("session") != session:
                        continue
                    after = rec.get("tokens_after")
                    if isinstance(after, int) and not isinstance(after, bool):
                        best = max(best, after)
        except OSError:
            return 0
        return best

    def _apps_ledger_record(self, rec: dict, *, result: Optional[dict],
                            exit_code: Optional[int], halted: Optional[str],
                            tokens: int, synthesized: bool,
                            spawned: bool = True, late: bool = False) -> dict:
        """One ledger line's fields, from the sidecar and result.json together."""
        result = result or {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else None
        # HOW LONG THE TURN TOOK, from the turn's OWN clock where it has one.
        # result.json's started_at/ended_at are the unit's; the sidecar's started_at
        # is the broker's spawn.
        seconds = None
        started = result.get("started_at") or rec.get("started_at")
        ended = result.get("ended_at")
        if started and ended:
            try:
                seconds = round((_dt.datetime.fromisoformat(str(ended))
                                 - _dt.datetime.fromisoformat(str(started))
                                 ).total_seconds(), 1)
            except (TypeError, ValueError):
                seconds = None
        if seconds is None and isinstance(rec.get("started_mono"), float):
            seconds = round(time.monotonic() - rec["started_mono"], 1)
        tokens_before = int(rec.get("tokens_before") or 0)
        return {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "session": rec.get("session"),
            "app": rec.get("app_id"),
            "turn": rec.get("turn"),
            "caller": rec.get("caller"),
            "unit": rec.get("unit"),
            "exit": exit_code,
            "seconds": seconds,
            "halted": halted,
            "no_changes": bool(result.get("no_changes")),
            "commit": result.get("commit"),
            "files": len(result.get("files") or []),
            "model": result.get("model") or rec.get("model"),
            "runner": result.get("runner") or self.apps.get("runner"),
            "usage": {
                "input": (usage or {}).get("input_tokens"),
                "output": (usage or {}).get("output_tokens"),
                "cache_read": (usage or {}).get("cache_read_input_tokens"),
                "cache_creation": (usage or {}).get("cache_creation_input_tokens"),
                "cost_usd": (usage or {}).get("total_cost_usd"),
            } if usage else None,
            "tokens": tokens,
            # Parent Round 6: the trip log record names the column it summed.
            "ceiling_column": "input+output+cache_creation",
            "tokens_after": tokens_before + tokens,
            "ceiling": self._apps_int("build_token_ceiling"),
            "synthesized": synthesized,
            # A ceiling refusal spawns nothing, so it is not a turn: git will never
            # write `turn N`, and the NEXT real handoff reuses N. The server keys
            # the turn counter off this too.
            "spawned": spawned,
            # True only on a record found AFTER its turn's halt was synthesized: the
            # turn finished, the room was already told it had not, and this line is
            # the contradiction on the record.
            "late": late,
        }

    # -- the sidecar -------------------------------------------------------

    def _apps_write_sidecar(self, rec: dict) -> None:
        """Persist what a FUTURE broker process needs to finish this turn's story."""
        path = self._apps_sidecar_path(rec["session"], rec["turn"])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in rec.items() if k != "started_mono"}, fh)

    def _apps_remove_sidecar(self, session: int, turn: int) -> None:
        try:
            os.unlink(self._apps_sidecar_path(session, turn))
        except OSError:
            pass

    # -- the verb ----------------------------------------------------------

    def _verb_apps_build(self, resident: str, args: dict) -> tuple[dict, str, dict]:
        """Hand one build prompt to the apps-builder seat for an open session."""
        self._apps_unavailable()
        _reject_unknown(args, {"session_id", "prompt_file"})
        if "session_id" not in args:
            raise _bad("session_id is required")
        session = _check_int(args, "session_id", 0, 1, 999_999_999)
        prompt_file = _check_str(args, "prompt_file", required=True, max_len=4000)
        assert prompt_file is not None

        # 1. the session, as the server knows it.
        view = self._apps_harness_view(session)
        if not view.get("open", False):
            raise VerbError("apps-refused", "this build session has ended")
        app_id = str(view.get("app_id") or "")
        if not APPS_APP_ID_RE.match(app_id):
            raise VerbError("exec-failure",
                            "the session's app id is not a valid app id")

        # 2. the seat -> bot map.
        seat_bots = self.apps.get("seat_bots") or {}
        mapped = seat_bots.get(resident)
        if not isinstance(mapped, int) or isinstance(mapped, bool):
            raise VerbError("apps-refused",
                            "this seat is not mapped to a builder bot")
        if mapped != view.get("builder_bot_id"):
            raise VerbError("apps-refused",
                            "this session belongs to another builder")

        self._apps_sweep_synthesized(app_id)

        # 3. the ceiling.
        ceiling = self._apps_int("build_token_ceiling")
        # The larger of what the SERVER was told and what this house LOGGED: a stage
        # post that never landed would otherwise buy a free turn against the
        # ceiling.
        tokens_used = max(int(view.get("tokens_used") or 0),
                          self._apps_ledger_tokens_after(session))
        turns = int(view.get("turns") or 0)
        turn = turns + 1
        if tokens_used >= ceiling:
            # Nothing spawns.
            detail = {"turn": turn, "halted": "ceiling", "spawned": False}
            self._apps_post_stage(session, "scoped", detail)
            self._apps_ledger(self._apps_ledger_record(
                {"session": session, "turn": turn, "app_id": app_id,
                 "caller": resident, "unit": apps_unit_name(session, turn),
                 "model": self.apps.get("model"), "tokens_before": tokens_used},
                result=None, exit_code=None, halted="ceiling", tokens=0,
                synthesized=False, spawned=False))
            raise VerbError("apps-refused",
                            f"this build has hit its token ceiling "
                            f"({tokens_used} of {ceiling})")

        # 4. one turn at a time per app.
        self._apps_claim(app_id, session, turn)
        try:
            prompt_path = self._map_resident_path(
                resident, prompt_file, label="prompt_file")
            self._apps_check_prompt(prompt_path)
            unit = apps_unit_name(session, turn)
            out_path = os.path.join(self._apps_log_dir(), f"{session}-{turn}.out")
            err_path = os.path.join(self._apps_log_dir(), f"{session}-{turn}.err")
            out_fh = self._apps_open_log(out_path)
            err_fh = self._apps_open_log(err_path)
            rec = {
                "schema": APPS_SIDECAR_SCHEMA,
                "session": session, "turn": turn, "app_id": app_id,
                "unit": unit, "caller": resident,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "deadline": time.time() + self.apps.get(
                    "turn_max_sec", APPS_TURN_MAX_SEC)
                + self._apps_num("result_grace_sec"),
                "out_path": out_path, "err_path": err_path,
                "tokens_before": tokens_used,
                "model": self.apps.get("model"),
                "started_mono": time.monotonic(),
            }
            try:
                self._apps_write_sidecar(rec)
                argv = [*self._apps_argv("launch_command"), resident,
                        str(session), str(turn), app_id, prompt_path]
                try:
                    proc = self._apps_spawn(argv, stdout=out_fh, stderr=err_fh)
                except OSError as exc:
                    # Never spawned — no unit, no turn, nothing to reap.
                    raise VerbError("exec-failure",
                                    f"the turn failed to launch: {exc}") from None
            finally:
                # The child holds its own dups; this process must not.
                self._close_build_logs(out_fh, err_fh)
        except BaseException:
            self._apps_release(app_id)
            self._apps_remove_sidecar(session, turn)
            raise

        # The launcher refuses before any privilege in milliseconds (exit 64: bad
        # charset, a path outside the caller's prompt dir, a symlink, the wrong
        # owner, an empty or oversized file).
        refusal = self._apps_early_refusal(proc, err_path)
        if refusal is not None:
            self._apps_release(app_id)
            self._apps_remove_sidecar(session, turn)
            self._unlink_build_logs(out_path, err_path)
            raise VerbError("apps-refused", refusal)

        self._apps_post_stage(session, "scoped",
                              {"turn": turn, "model": self.apps.get("model")})
        t = threading.Thread(target=self._reap_apps, args=(rec, proc), daemon=True)
        self._apps_threads.append(t)
        t.start()
        return ({"turn": turn, "unit": unit, "app_id": app_id},
                f"apps-build turn {turn} for session {session} launched as {unit}",
                {"session": session, "turn": turn, "unit": unit,
                 "app_id": app_id})

    def _apps_open_log(self, path: str) -> Any:
        """The launcher's own stdout/stderr, 0600."""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError as exc:
            raise VerbError("exec-failure",
                            f"cannot create the turn's output file: {exc}") from None
        return os.fdopen(fd, "wb")

    def _apps_check_prompt(self, path: str) -> None:
        """The prompt, read as bytes and bounded — a COURTESY CHECK, not the wall."""
        bound = self._apps_int("prompt_max_bytes")
        try:
            with open(path, "rb") as fh:
                blob = fh.read(bound + 1)
        except OSError:
            raise VerbError("apps-refused",
                            "the prompt file cannot be read") from None
        if not blob.strip():
            raise VerbError("apps-refused", "the prompt file is empty")
        if len(blob) > bound:
            raise VerbError("apps-refused",
                            f"the prompt file is larger than {bound} bytes")
        markers = self.apps.get("chat_markers") or APPS_DEFAULTS["chat_markers"]
        for marker in markers:
            if isinstance(marker, str) and marker and marker.encode() in blob:
                raise VerbError("apps-refused", APPS_CHAT_MARKER_REFUSAL)

    def _apps_early_refusal(self, proc: Any, err_path: str) -> Optional[str]:
        """The launcher's pre-privilege refusal, or None if the turn is under way."""
        try:
            rc = proc.wait(timeout=APPS_SPAWN_CHECK_SEC)
        except subprocess.TimeoutExpired:
            return None            # still running: the turn is under way
        except Exception:  # noqa: BLE001 — a probe never sinks a launched turn
            return None
        if rc == 0 or rc is None:
            return None
        tail = self._read_build_tail(err_path).strip().splitlines()
        last = tail[-1].strip()[:200] if tail else ""
        if rc == APPS_LAUNCH_REFUSED_EXIT:
            return f"the launcher refused the turn: {last}" if last else \
                "the launcher refused the turn (exit 64)"
        return (f"the turn could not be launched (exit {rc})"
                + (f": {last}" if last else ""))

    # -- the reaper --------------------------------------------------------

    def _reap_apps(self, rec: dict, proc: Any = None) -> None:
        """Watch one turn to its terminal record, publish it, log it, let go."""
        session, turn = int(rec["session"]), int(rec["turn"])
        turn_dir = self._apps_turn_dir(session, turn)
        result_path = os.path.join(turn_dir, "result.json")
        grace = self._apps_num("result_grace_sec")
        poll = max(0.01, self._apps_num("poll_sec"))
        scaffolded = False
        ended_at: Optional[float] = None      # when the process/unit went away
        unparseable_since: Optional[float] = None
        # Slice (iv): one harness-view read per `stop_poll_sec` while the unit is
        # alive and no stop has been sent yet.
        view_every = max(poll, self._apps_num("stop_poll_sec"))
        last_view = time.monotonic() - view_every
        stale_seen = False
        try:
            while not self._closed:
                if not scaffolded and os.path.exists(
                        os.path.join(turn_dir, "scaffolded")):
                    self._apps_post_stage(session, "scaffolded", {"turn": turn})
                    scaffolded = True
                result, bad = self._apps_read_result(result_path)
                if result is not None and not self._apps_result_is_ours(rec, result):
                    if not stale_seen:
                        stale_seen = True
                        self._audit("broker", "apps-build",
                                    {"session": session, "turn": turn}, True,
                                    f"ignoring a result.json that is not this "
                                    f"turn's (app {result.get('app_id')!r}, "
                                    f"session {result.get('session')!r}, turn "
                                    f"{result.get('turn')!r}); waiting for the "
                                    f"real harvest")
                    result = None
                if result is not None:
                    self._apps_finish(rec, result, proc, scaffolded)
                    return
                if bad:
                    # A half-written file the harvest is still renaming into place:
                    # re-read.
                    now = time.monotonic()
                    unparseable_since = unparseable_since or now
                    if now - unparseable_since <= grace:
                        time.sleep(poll)
                        continue
                if proc is not None:
                    alive = proc.poll() is None
                else:
                    alive = self._apps_unit_state(
                        str(rec.get("unit"))) in BUILD_ACTIVE_STATES
                # THE USER'S STOP (slice (iv)).
                if alive and not (rec.get("stop_sent") or rec.get("stop_abandoned")):
                    now = time.monotonic()
                    if now - last_view >= view_every:
                        last_view = now
                        if self._apps_stop_requested(session):
                            if self._apps_send_stop(rec):
                                rec["stop_sent"] = True
                            else:
                                tries = int(rec.get("stop_refusals") or 0) + 1
                                rec["stop_refusals"] = tries
                                if tries >= APPS_STOP_MAX_REFUSALS:
                                    rec["stop_abandoned"] = True
                                    self._audit(
                                        "broker", "apps-build",
                                        {"session": session, "turn": turn},
                                        True,
                                        f"stop abandoned for {rec.get('unit')}: "
                                        f"the launcher refused {tries} times, "
                                        f"which is permanent; the turn runs to "
                                        f"its own clock")
                            self._apps_write_sidecar(rec)
                if not alive:
                    now = time.monotonic()
                    ended_at = ended_at if ended_at is not None else now
                    if now - ended_at >= grace:
                        self._apps_synthesize(
                            rec, proc, scaffolded,
                            "the turn ended without a result")
                        return
                if time.time() > float(rec.get("deadline") or 0):
                    # THE DEADLINE MUST NOT FIRE OVER A LIVE UNIT.
                    hard = (float(rec.get("deadline") or 0)
                            + self._apps_num("unit_stop_timeout_sec"))
                    if not alive:
                        self._apps_synthesize(rec, proc, scaffolded,
                                              "the turn passed its deadline")
                        return
                    if time.time() > hard:
                        self._apps_synthesize(
                            rec, proc, scaffolded,
                            "the turn passed its deadline and its unit did "
                            "not stop")
                        return
                time.sleep(poll)
        except Exception as exc:  # noqa: BLE001 — never die silently
            self._audit("broker", "apps-build",
                        {"session": session, "turn": turn}, True,
                        f"the apps reaper failed: {exc!r}")
            self._apps_release(str(rec.get("app_id") or ""))
            self._apps_remove_sidecar(session, turn)
            return
        # Shutting down: leave the sidecar exactly where the NEXT process looks for
        # it.

    @staticmethod
    def _apps_result_is_ours(rec: dict, result: dict) -> bool:
        """Does this result.json describe the turn on this ticket?"""
        try:
            if str(result.get("app_id") or "") != str(rec.get("app_id") or ""):
                return False
            if int(result.get("session", -1)) != int(rec["session"]):
                return False
            if int(result.get("turn", -1)) != int(rec["turn"]):
                return False
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _apps_read_result(path: str) -> tuple[Optional[dict], bool]:
        """(record, unparseable)."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None, False
        except (OSError, json.JSONDecodeError):
            return None, True
        return (data, False) if isinstance(data, dict) else (None, True)

    def _apps_last_stage(self, rec: dict, scaffolded: bool) -> str:
        """Where the bar is standing when a turn halts."""
        if scaffolded or os.path.exists(
                os.path.join(self._apps_turn_dir(int(rec["session"]),
                                                 int(rec["turn"])), "scaffolded")):
            return "scaffolded"
        return "scoped"

    def _apps_finish(self, rec: dict, result: dict, proc: Any,
                     scaffolded: bool) -> None:
        """The terminal record exists: publish it, flag what needs a human, log it,
        release."""
        session, turn = int(rec["session"]), int(rec["turn"])
        halted = result.get("halted")
        error = apps_clean_line(result.get("error"))
        tokens = apps_tokens(result.get("usage"))
        files = apps_cap_files(result.get("files"))
        model = result.get("model") or rec.get("model")
        summary = apps_clean_line(result.get("summary"))
        exit_code = result.get("exit")
        exit_code = exit_code if isinstance(exit_code, int) else None
        if halted or error:
            halted = str(halted) if halted else "error"
            detail = {"turn": turn, "halted": halted, "files": files,
                      "tokens": tokens, "model": model}
            if error:
                detail["reason"] = error
            # A turn that wrote a report and THEN failed still has a report worth
            # reading; forward it so the room's halt line can quote it.
            if summary:
                detail["summary"] = summary
            self._apps_post_stage(session, self._apps_last_stage(rec, scaffolded),
                                  detail)
            if halted == "secret":
                # §E: a turn that tried to publish a credential has earned a human
                # before the next one.
                self._narrate(
                    f"FLAG apps-build: session {session} turn {turn} "
                    f"(app {rec.get('app_id')}, caller {rec.get('caller')}) "
                    "tried to write a credential — quarantined at "
                    f"{result.get('quarantine')}; the session was closed by the "
                    "server.")
        else:
            halted = None
            detail = {"turn": turn, "files": files, "tokens": tokens,
                      "model": model, "no_changes": bool(result.get("no_changes"))}
            if summary:
                detail["summary"] = summary
            self._apps_post_stage(session, "files_written", detail)
            if result.get("commit") and not result.get("no_changes"):
                self._apps_post_stage(session, "deployed", {"turn": turn})
        flag = apps_clean_line(result.get("flag"))
        if flag:
            # The builder never talks to the user: a flag goes to the admin in one
            # line and the build continues (parent "Flagging").
            self._narrate(f"FLAG apps-build: session {session} turn {turn} "
                          f"(app {rec.get('app_id')}): {flag}")
        self._apps_ledger(self._apps_ledger_record(
            rec, result=result, exit_code=exit_code, halted=halted,
            tokens=tokens, synthesized=False))
        self._apps_release(str(rec.get("app_id") or ""))
        self._apps_remove_sidecar(session, turn)

    def _apps_synthesize(self, rec: dict, proc: Any, scaffolded: bool,
                         reason: str) -> None:
        """The absence branch (§E): a unit that ended with no result.json is a HALT."""
        session, turn = int(rec["session"]), int(rec["turn"])
        exit_code = getattr(proc, "returncode", None) if proc is not None else None
        self._apps_post_stage(
            session, self._apps_last_stage(rec, scaffolded),
            {"turn": turn, "halted": "error", "reason": reason})
        self._apps_ledger(self._apps_ledger_record(
            rec, result=None,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            halted="error", tokens=0, synthesized=True))
        self._apps_release(str(rec.get("app_id") or ""))
        rec = {**rec, "synthesized": True,
               "synthesized_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        try:
            self._apps_write_sidecar(rec)
        except OSError as exc:
            self._audit("broker", "apps-build",
                        {"session": session, "turn": turn}, True,
                        f"could not mark the synthesized ticket: {exc}")
            self._apps_remove_sidecar(session, turn)

    def _apps_sweep_synthesized(self, app_id: str) -> None:
        """Resolve this app's marked tickets before its next turn starts."""
        try:
            entries = sorted(os.listdir(self._apps_log_dir()))
        except OSError:
            return
        for name in entries:
            if not name.endswith(APPS_SIDECAR_SUFFIX):
                continue
            path = os.path.join(self._apps_log_dir(), name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                if not rec.get("synthesized"):
                    continue
                if str(rec.get("app_id") or "") != app_id:
                    continue
                self._apps_resolve_synthesized(rec)
            except Exception as exc:  # noqa: BLE001 — never refuse over a sweep
                self._audit("broker", "apps-build", {"app_id": app_id}, True,
                            f"could not sweep a synthesized ticket: {exc!r}")

    def _apps_resolve_synthesized(self, rec: dict) -> None:
        """Close out a turn whose halt this house SYNTHESIZED."""
        session, turn = int(rec["session"]), int(rec["turn"])
        result, bad = self._apps_read_result(
            os.path.join(self._apps_turn_dir(session, turn), "result.json"))
        if result is not None and not self._apps_result_is_ours(rec, result):
            self._audit(
                "broker", "apps-build", {"session": session, "turn": turn},
                True,
                "a result.json in this turn's dir is not this turn's (app "
                f"{result.get('app_id')!r}, session {result.get('session')!r}, "
                f"turn {result.get('turn')!r}) — nothing ledgered, ticket "
                "dropped")
            result = None
        if bad:
            # A late record that will not parse is still a fact about this turn, and
            # dropping it with its ticket would leave no line anywhere.
            self._audit(
                "broker", "apps-build", {"session": session, "turn": turn},
                True,
                "a result landed after this turn's halt was synthesized but "
                "would not parse — nothing ledgered, ticket dropped")
        if result is not None:
            exit_code = result.get("exit")
            self._apps_ledger(self._apps_ledger_record(
                rec, result=result,
                exit_code=exit_code if isinstance(exit_code, int) else None,
                halted=(str(result.get("halted")) if result.get("halted")
                        else None),
                tokens=apps_tokens(result.get("usage")),
                synthesized=False, late=True))
            self._audit(
                "broker", "apps-build", {"session": session, "turn": turn},
                True,
                "a result landed after this turn's halt was synthesized — "
                f"ledgered late (commit {result.get('commit')})")
        self._apps_remove_sidecar(session, turn)

    # -- reattachment after a restart --------------------------------------

    def adopt_inflight_apps(self) -> list[str]:
        """Re-adopt app build turns that outlived the previous broker process."""
        adopted: list[str] = []
        if not self.apps_configured:
            return adopted
        try:
            entries = sorted(os.listdir(self._apps_log_dir()))
        except OSError:
            return adopted
        for name in entries:
            if not name.endswith(APPS_SIDECAR_SUFFIX):
                continue
            path = os.path.join(self._apps_log_dir(), name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                session, turn = int(rec["session"]), int(rec["turn"])
                # The ticket must be named after the turn it claims, or the ids
                # inside decide what gets published while the filename decides what
                # gets deleted.
                if name != f"{session}-{turn}{APPS_SIDECAR_SUFFIX}":
                    raise ValueError("sidecar name does not match its turn")
                rec["unit"] = rec.get("unit") or apps_unit_name(session, turn)
            except Exception:  # noqa: BLE001 — an unreadable ticket is garbage
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            if rec.get("synthesized"):
                # This turn already got its synthesized halt in the room.
                self._apps_resolve_synthesized(rec)
                continue
            app_id = str(rec.get("app_id") or "")
            with self._apps_lock:
                if app_id in self._active_apps:
                    continue        # this process already owns a turn on this app
                self._active_apps[app_id] = (session, turn)
            adopted.append(str(rec["unit"]))
            t = threading.Thread(target=self._reap_apps, args=(rec, None),
                                 daemon=True)
            self._apps_threads.append(t)
            t.start()
        return adopted

    def join_apps(self, timeout: float = 10.0) -> None:
        """Join the apps reaper threads — TEST convenience only."""
        for t in list(self._apps_threads):
            t.join(timeout)

    # ---------------------------------------------------------------- wake

    def _check_wake_identity(self, resident: str, verb: str) -> Optional[str]:
        """None if this identity may attempt this verb, else the refusal text."""
        is_waker = resident in self.wake_callers
        if verb == WAKE_VERB and not is_waker:
            return (f"{resident} may not wake anyone — the wake caller is "
                    "authenticated by uid at the socket, and no seat is one")
        if is_waker and verb != WAKE_VERB:
            return (f"{resident} is a wake caller and may call only "
                    f"{WAKE_VERB!r}")
        return None

    def _wake_spool_dir(self) -> str:
        """The spool realpath VERIFIED resident-unwritable at construction — never
        the raw config string, for the reason _specs_dir prefers its verified path:
        the directory written must be the directory proven."""
        if not self.wake_spool_real:
            raise VerbError("internal", "wake.spool_dir is not configured")
        return self.wake_spool_real

    def _write_wake_record(self, record: dict) -> str:
        """One 0644 JSON record per wake, written atomically."""
        path = os.path.join(self._wake_spool_dir(),
                            record["wake_id"] + WAKE_SPOOL_SUFFIX)
        fd, tmp = tempfile.mkstemp(dir=self._wake_spool_dir(),
                                   prefix=".wake-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False)
                fh.write("\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def _read_wake_records(self) -> list[dict]:
        """Every parseable record in the spool."""
        try:
            names = sorted(os.listdir(self._wake_spool_dir()))
        except (OSError, VerbError):
            return []
        out: list[dict] = []
        for name in names:
            if not name.endswith(WAKE_SPOOL_SUFFIX):
                continue
            try:
                with open(os.path.join(self._wake_spool_dir(), name), "r",
                          encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict) and _WAKE_ID_RE.match(str(rec.get("wake_id"))):
                out.append(rec)
        return out

    @staticmethod
    def _wake_requested_epoch(record: dict) -> Optional[float]:
        """When the wake was asked for."""
        try:
            started = _dt.datetime.fromisoformat(str(record.get("requested_at")))
        except (TypeError, ValueError):
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=_dt.timezone.utc)
        return started.timestamp()

    @classmethod
    def _wake_window_ends(cls, record: dict) -> Optional[float]:
        """When this wake stops being in flight: requested_at + the session cap +
        the grace margin."""
        started = cls._wake_requested_epoch(record)
        if started is None:
            return None
        cap = record.get("session_cap_sec")
        grace = record.get("grace_sec")
        cap = cap if isinstance(cap, int) and cap > 0 else DEFAULT_WAKE_SESSION_CAP_SEC
        grace = grace if isinstance(grace, int) and grace >= 0 else DEFAULT_WAKE_GRACE_SEC
        return started + cap + grace

    def _prune_wake_spool(self, now: Optional[float] = None) -> int:
        """Delete records past the RETENTION horizon (not past their window)."""
        now = now if now is not None else time.time()
        removed = 0
        for rec in self._read_wake_records():
            started = self._wake_requested_epoch(rec)
            if started is not None and started + WAKE_RETENTION_SEC > now:
                continue
            try:
                os.unlink(os.path.join(self._wake_spool_dir(),
                                       str(rec["wake_id"]) + WAKE_SPOOL_SUFFIX))
                removed += 1
            except OSError:
                pass
        return removed

    def _daily_wake_cap(self) -> int:
        """`[wake].daily_wake_cap`, else DEFAULT_DAILY_WAKE_CAP."""
        cap = self.wake.get("daily_wake_cap", DEFAULT_DAILY_WAKE_CAP)
        if isinstance(cap, bool) or not isinstance(cap, int):
            return DEFAULT_DAILY_WAKE_CAP
        return cap

    def _wake_spend_today(self, seat: str, now: float) -> tuple[int, float]:
        """(wakes, session seconds) recorded for this seat so far today (UTC)."""
        count = 0
        spent = 0.0
        today = _dt.datetime.fromtimestamp(
            now, _dt.timezone.utc).strftime("%Y-%m-%d")
        for rec in self._read_wake_records():
            if rec.get("resident") != seat:
                continue
            started = self._wake_requested_epoch(rec)
            if started is None:
                continue
            if _dt.datetime.fromtimestamp(
                    started, _dt.timezone.utc).strftime("%Y-%m-%d") != today:
                continue
            granted = rec.get("session_cap_sec")
            if isinstance(granted, bool) or not isinstance(granted, int):
                granted = 0
            count += 1
            spent += max(0.0, min(float(granted), now - started))
        return count, spent

    def _active_wake(self, resident: str) -> Optional[dict]:
        """The most recent wake still in flight for this seat, if any."""
        if not self.wake:
            return None
        now = time.time()
        live = [r for r in self._read_wake_records()
                if r.get("resident") == resident
                and (self._wake_window_ends(r) or 0) > now]
        if not live:
            return None
        return max(live, key=lambda r: str(r.get("requested_at", "")))

    def _assert_woken_build_allowed(self, resident: str, wake: dict,
                                    text: str) -> None:
        """The no-self-review rule, one level down (spec)."""
        raw = parse_review_owner(text)
        if raw is None:
            raise _bad(
                f"woken session ({wake.get('wake_id')}): this spec states no "
                "review owner, so it cannot be shown that the review does not "
                "land in the building seat's own queue — a woken build needs a "
                "'Review owner' line")
        owner_seat = review_owner_seat(raw, self.seat_names)
        if owner_seat is None:
            return
        if owner_seat == resident:
            raise _bad(
                f"woken session ({wake.get('wake_id')}): this spec's review "
                f"owner is {raw!r}, which is this seat — a seat may not build "
                "what it would then review")
        woken_by = str(wake.get("woken_by") or "")
        if woken_by in self.seat_names and owner_seat == woken_by:
            raise _bad(
                f"woken session ({wake.get('wake_id')}): this spec's review "
                f"owner is {raw!r}, which is the seat that woke this one — the "
                "review would land in the waker's queue")

    def _verb_wake(self, caller: str, args: dict) -> tuple[dict, str, dict]:
        """Wake a seat with a task (SPECS/2026-08-25-agentic-residents.md)."""
        _reject_unknown(args, {"resident", "task"})
        seat = _check_str(args, "resident", required=True, max_len=64)
        task = _check_str(args, "task", required=True,
                          max_len=MAX_WAKE_TASK_CHARS)
        assert seat is not None and task is not None
        if seat not in self.wake_seats:
            raise _bad(f"{seat!r} is not a wakeable seat "
                       f"({', '.join(sorted(self.wake_seats)) or 'none'} are)")
        if not task.strip():
            raise _bad("task must not be empty — a wake names the work")

        now = _dt.datetime.now(_dt.timezone.utc)
        cap = self._daily_wake_cap()
        record = {
            "schema": WAKE_SPOOL_SCHEMA,
            "wake_id": new_wake_id(now),
            "resident": seat,
            "woken_by": caller,
            "requested_at": now.isoformat(),
            "session_cap_sec": self._wake_session_cap(),
            "grace_sec": self._wake_grace(),
            "task": task,
        }
        with self._wake_lock:
            count, spent = self._wake_spend_today(seat, now.timestamp())
            if count >= cap:
                raise VerbError("over-budget", format_wake_refusal(
                    seat=seat, count=count, cap=cap, spent_sec=spent))
            try:
                self._write_wake_record(record)
            except OSError as exc:
                raise VerbError("exec-failure",
                                f"cannot record the wake: {exc}") from None
        self._prune_wake_spool()

        return (
            {"wake_id": record["wake_id"], "resident": seat,
             "session_cap_sec": record["session_cap_sec"],
             "grace_sec": record["grace_sec"],
             "requested_at": record["requested_at"]},
            f"wake {record['wake_id']} recorded for {seat} by {caller} "
            f"(cap {record['session_cap_sec']}s)",
            # A FACT field, like start-build's `build_started`: the wake id is what
            # ties this line to the action log's start/end pair and to the
            # #custodian post the seat's runner makes.
            {"wake_id": record["wake_id"]},
        )

    # ------------------------------------------- resident path translation

    def _map_resident_path(self, resident: str, path: str,
                           label: str = "path") -> str:
        """One container path, translated to the host path it means — and refused if
        it means nothing."""
        path_map = self.residents.get(resident, {}).get("path_map")
        if not path_map:
            raise _bad(f"no path_map configured for {resident}; a {label} must "
                       "resolve through an explicit allowlist")
        best = max((p for p in path_map
                    if path == p or path.startswith(p.rstrip("/") + "/")),
                   key=len, default=None)
        if best is None:
            raise _bad(f"{label} is not under a mapped root for {resident}; "
                       f"available roots: {sorted(path_map)}")
        return path_map[best].rstrip("/") + path[len(best.rstrip("/")):]

    def _verb_classify_diff(self, resident: str, args: dict) -> tuple[dict, str]:
        """Contract with harness/classifier/classify_diff.py (WP-H4): argv:
        <classify_diff.py> --repo <abs path> --range <git range> --config
        <protected-paths.toml> --gates <json object>; stdout: one JSON object (the
        classification), exit 0."""
        _reject_unknown(args, {"repo", "range", "gates"})
        repo = _check_str(args, "repo", required=True, max_len=300)
        assert repo is not None
        if not repo.startswith("/") or "/../" in repo or repo.endswith("/.."):
            raise _bad("repo must be an absolute path without ..")
        rng = _check_str(args, "range", required=True, max_len=200)
        assert rng is not None
        if rng.startswith("-") or not _RANGE_RE.match(rng):
            raise _bad("range must be a plain git rev/range "
                       "(letters, digits, . _ ~ ^ / { } -, no leading dash)")
        # WP-H13 F3: the classifier splits A..B (or A...B) and hands each side to
        # git as a bare positional.
        for _side in rng.replace("...", "..").split(".."):
            if _side.startswith("-"):
                raise _bad("neither side of the range may start with '-'")
        repo = self._map_resident_path(resident, repo, label="repo")
        gates = args.get("gates", {})
        if not isinstance(gates, dict):
            raise _bad("gates must be an object")
        gates_json = json.dumps(gates, ensure_ascii=False)
        if len(gates_json) > MAX_GATES_JSON:
            raise _bad(f"gates JSON exceeds {MAX_GATES_JSON} bytes")
        classifier = self.paths.get(
            "classifier",
            "/home/plink/Disjorn/Disjorn/harness/classifier/classify_diff.py")
        protected = self.paths.get(
            "protected_paths",
            "/home/plink/Disjorn/Disjorn/harness/classifier/protected-paths.toml")
        argv = self._argv("classify_diff", [sys.executable, classifier])
        argv += ["--repo", repo, "--range", rng,
                 "--config", protected, "--gates", gates_json]
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["classify-diff"])
        if cp.returncode != 0:
            raise VerbError("exec-failure",
                            f"classifier exit {cp.returncode}: {cp.stderr.strip()[:500]}")
        try:
            classification = json.loads(cp.stdout)
        except json.JSONDecodeError:
            raise VerbError("exec-failure", "classifier emitted non-JSON output") from None
        tier = classification.get("tier") if isinstance(classification, dict) else None
        return ({"classification": classification}, f"classified: tier={tier}")

    def _verb_read_prod_logs(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, {"lines"})
        lines = _check_int(args, "lines", 100, 1, MAX_LOG_LINES)
        argv = self._argv("read_prod_logs",
                          ["journalctl", "-u", "disjorn", "--no-pager", "-o", "short-iso"])
        argv += ["-n", str(lines)]
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["read-prod-logs"])
        if cp.returncode != 0:
            raise VerbError("exec-failure",
                            f"journalctl exit {cp.returncode}: {cp.stderr.strip()[:300]}")
        out = cp.stdout.splitlines()[-lines:]
        return ({"lines": out}, f"{len(out)} lines")

    def _verb_read_own_log(self, resident: str, args: dict) -> tuple[dict, str]:
        """Tail/grep of the CALLING resident's configured log file only."""
        _reject_unknown(args, {"lines", "grep", "path"})
        lines = _check_int(args, "lines", 100, 1, MAX_LOG_LINES)
        grep = _check_str(args, "grep", max_len=MAX_GREP_CHARS)
        cfg_path = self.residents.get(resident, {}).get("log_path")
        if not cfg_path:
            raise VerbError("internal", f"no log_path configured for {resident}")
        requested = _check_str(args, "path", max_len=500)
        if requested is not None and os.path.realpath(requested) != os.path.realpath(cfg_path):
            raise _bad("path may only be this resident's configured log file")
        try:
            with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.read().splitlines()
        except OSError as exc:
            raise VerbError("exec-failure", f"log not readable: {exc}") from None
        if grep is not None:
            all_lines = [ln for ln in all_lines if grep in ln]
        tail = all_lines[-lines:]
        return ({"lines": tail, "path": cfg_path},
                f"{len(tail)} lines" + (f" (grep={grep!r})" if grep else ""))

    def _verb_read_metrics(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, set())
        path = self.paths.get("metrics_json")
        if not path:
            raise VerbError("internal", "paths.metrics_json not configured")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                metrics = json.load(fh)
        except OSError as exc:
            raise VerbError("exec-failure", f"metrics not readable: {exc}") from None
        except json.JSONDecodeError:
            raise VerbError("exec-failure", "metrics file is not valid JSON") from None
        return ({"metrics": metrics}, "metrics read")

    def _verb_file_proposal(self, resident: str, args: dict) -> tuple[dict, str]:
        _reject_unknown(args, {"text"})
        text = _check_str(args, "text", required=True, max_len=MAX_PROPOSAL_CHARS)
        assert text is not None
        body = f"[proposal from {resident}] {text}"
        try:
            posted = self.transport(self.disjorn, body)
        except VerbError:
            raise
        except Exception as exc:  # noqa: BLE001 — transport errors -> clean failure
            raise VerbError("exec-failure", f"proposal post failed: {exc}") from None
        return ({"posted": True, **(posted or {})},
                f"proposal posted ({len(text)} chars)")

    def _verb_query_own_audit(self, resident: str, args: dict) -> tuple[dict, str]:
        """The calling resident's OWN audit lines for a date range."""
        _reject_unknown(args, {"date_from", "date_to", "limit"})
        date_from = _check_date(args, "date_from")
        date_to = _check_date(args, "date_to")
        limit = _check_int(args, "limit", 100, 1, MAX_AUDIT_ENTRIES)
        entries: list[dict] = []
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("resident") != resident:
                        continue
                    day = str(rec.get("ts", ""))[:10]
                    if date_from <= day <= date_to:
                        entries.append(rec)
        except OSError as exc:
            raise VerbError("exec-failure", f"audit log not readable: {exc}") from None
        tail = entries[-limit:]  # most recent within range
        return ({"entries": tail, "count": len(tail),
                 "truncated": len(entries) > limit},
                f"{len(tail)} audit entries")

    # ------------------------------------------------------- summon hops

    def _work_item_bucket(self, work_item: Optional[str]) -> Optional[str]:
        """The work item this chain spends against, or None for no bucket."""
        if not work_item or self.hops is None:
            return None
        if not BOARD_SLUG_RE.match(work_item):
            return None
        try:
            body = self._board_get(f"/planroom/cards/{work_item}")
        except VerbError:
            return None
        card = body.get("card") or {}
        return work_item if card.get("column") == "Review" else None

    def _verb_summon_hop(self, resident: str, args: dict) -> tuple[dict, str]:
        """The bot-to-bot hop wall, for the summon adapters."""
        _reject_unknown(args, {"action", "work_item", "summoner", "seq"})
        action = _check_str(args, "action", required=True, max_len=10)
        if action not in ("spend", "unpark"):
            raise _bad("action must be 'spend' or 'unpark'")
        work_item = _check_str(args, "work_item", max_len=80)
        summoner = _check_str(args, "summoner", max_len=100) or "someone"
        seq = args.get("seq")
        if seq is not None:
            seq = _check_int(args, "seq", 0, 0, 2**53)

        if action == "unpark":
            if not work_item:
                raise _bad("unpark needs a work_item")
            if self.hops is None:
                return ({"reset": False, "reason": "no-wall"},
                        "no hop wall configured")
            out = self.hops.unpark(work_item, seq)
            verb = ("unparked by" if out["reset"]
                    else "already unparked, reported again by")
            return ({"work_item": work_item, **out},
                    f"{work_item} {verb} {summoner}")

        bucket = self._work_item_bucket(work_item)
        if bucket is None:
            return ({"allowed": True, "chain": False, "work_item": work_item,
                     "reason": "no-bucket"},
                    f"depth-1 only for {summoner} (no live work item)")
        out = self.hops.spend(bucket)
        result = {"chain": bool(out["allowed"]), "work_item": bucket, **out}
        summary = (f"hop {out['count']}/{out['cap']} on {bucket} for {summoner}"
                   if out["allowed"] else
                   f"REFUSED {out['reason']} {out['count']}/{out['cap']} on {bucket}")
        return (result, summary)

    # ---------------------------------------------------------- plan room
    # Five verbs. Three read, two write, and the two writes touch BOARD-NATIVE STATE
    # ONLY: comments and the blocked flag + its reason. They are structurally unable
    # to touch derived state — not because anything here checks, but because derived
    # state has no write path anywhere in this house (P1).

    def _board_slug(self, args: dict) -> str:
        slug = _check_str(args, "slug", required=True, max_len=80)
        assert slug is not None
        if not BOARD_SLUG_RE.match(slug):
            raise _bad("slug must be a spec slug (YYYY-MM-DD-name), a "
                       "`backlog-<n>`, or a `keyboard-<sha>`")
        return slug

    def _board_get(self, path: str) -> dict:
        return self.planroom_api(self.disjorn, "GET", path)

    def _board_post(self, path: str, payload: dict) -> dict:
        return self.planroom_api(self.disjorn, "POST", path, payload)

    def _verb_board_list(self, resident: str, args: dict) -> tuple[dict, str]:
        """The board, ONE LINE PER CARD."""
        _reject_unknown(args, {"column", "lane", "owner", "blocked", "limit"})
        query: list[str] = []
        for key in ("column", "lane", "owner"):
            val = _check_str(args, key, max_len=100)
            if val is not None:
                query.append(f"{key}={_urlq(val)}")
        blocked = _check_str(args, "blocked", max_len=8)
        if blocked is not None:
            if blocked not in ("yes", "no"):
                raise _bad("blocked must be 'yes' or 'no'")
            query.append(f"blocked={'true' if blocked == 'yes' else 'false'}")
        limit = _check_int(args, "limit", 80, 1, MAX_BOARD_CARDS)
        qs = ("?" + "&".join(query)) if query else ""
        body = self._board_get("/planroom/board" + qs)
        cards = body.get("cards") or []
        lines = [format_board_line(c) for c in cards[:limit]]
        return ({"face": format_board_face(body.get("face") or {}),
                 "counts": body.get("counts") or {},
                 "cards": lines, "count": len(lines),
                 "truncated": len(cards) > limit},
                f"{len(lines)} of {len(cards)} cards")

    def _verb_board_card(self, resident: str, args: dict) -> tuple[dict, str]:
        """Everything on one card, comments included."""
        _reject_unknown(args, {"slug"})
        slug = self._board_slug(args)
        body = self._board_get(f"/planroom/cards/{slug}")
        comments = body.get("comments") or []
        return ({"face": format_board_face(body.get("face") or {}),
                 "card": body.get("card"), "comments": comments,
                 "note": body.get("note")},
                f"card {slug} ({len(comments)} comment(s))")

    def _verb_board_search(self, resident: str, args: dict) -> tuple[dict, str]:
        """Substring search across card text and comments, one line per hit."""
        _reject_unknown(args, {"text", "limit"})
        text = _check_str(args, "text", required=True,
                          max_len=MAX_BOARD_SEARCH_CHARS)
        assert text is not None
        limit = _check_int(args, "limit", 40, 1, MAX_BOARD_CARDS)
        body = self._board_get(
            f"/planroom/search?q={_urlq(text)}&limit={limit}")
        cards = body.get("cards") or []
        return ({"face": format_board_face(body.get("face") or {}),
                 "cards": [format_board_line(c) for c in cards],
                 "count": len(cards), "truncated": bool(body.get("truncated"))},
                f"{len(cards)} hits for {text!r}")

    def _verb_board_flag(self, resident: str, args: dict) -> tuple[dict, str]:
        """Block or unblock a card, with a reason."""
        _reject_unknown(args, {"slug", "action", "reason"})
        slug = self._board_slug(args)
        action = _check_str(args, "action", required=True, max_len=20)
        if action not in ("blocked", "unblock"):
            raise _bad("action must be 'blocked' or 'unblock'")
        reason = _check_str(args, "reason", max_len=MAX_BOARD_REASON_CHARS)
        blocked = action == "blocked"
        if blocked and not (reason or "").strip():
            raise _bad("blocking a card needs a reason — a card blocked for no "
                       "stated reason is one nobody can unblock")
        body = self._board_post(f"/planroom/cards/{slug}/flag",
                                {"blocked": blocked, "reason": reason,
                                 "author": resident})
        card = body.get("card") or {}
        return ({"slug": slug, "blocked": bool(card.get("blocked")),
                 "reason": card.get("blocked_reason"),
                 "column": card.get("column"),
                 "card": format_board_line(card) if card else None},
                f"{slug} {'blocked' if blocked else 'unblocked'}"
                + (f": {reason[:120]}" if blocked and reason else ""))

    def _verb_board_comment(self, resident: str, args: dict) -> tuple[dict, str]:
        """Add a comment to a card."""
        _reject_unknown(args, {"slug", "text"})
        slug = self._board_slug(args)
        text = _check_str(args, "text", required=True,
                          max_len=MAX_BOARD_COMMENT_CHARS)
        assert text is not None
        body = self._board_post(f"/planroom/cards/{slug}/comment",
                                {"text": text, "author": resident})
        comment = body.get("comment") or {}
        return ({"slug": slug, "comment": comment},
                f"comment on {slug} ({len(text)} chars)")

    # ------------------------------------------------- plan room: rebuilds

    def _planroom_index_path(self) -> Optional[str]:
        path = self.planroom.get("index")
        return path if isinstance(path, str) and path else None

    def _planroom_rebuild(self, why: str) -> dict:
        """Re-derive the board and rewrite the index."""
        index_path = self._planroom_index_path()
        if not index_path:
            return {"rebuilt": False, "reason": "no [planroom].index configured"}
        if not self._planroom_lock.acquire(blocking=False):
            # Another rebuild is already in flight and will see the same world.
            return {"rebuilt": False, "reason": "a rebuild is already running"}
        try:
            planroom = _load_planroom_module()
            data = planroom.derive_cards(
                self.config, lane_owners=self.planroom.get("lane_owners"))
            lines = planroom.rebuild(index_path, data)
        except Exception as exc:  # noqa: BLE001 — never let a cache take the
            # daemon, or a verb, down with it.
            return {"rebuilt": False, "reason": f"{type(exc).__name__}: {exc}"}
        finally:
            self._planroom_lock.release()
        if lines and self.planroom.get("announce", True):
            # ONE SYSTEM LINE PER COLUMN TRANSITION, NEVER PER EDIT.
            self._narrate("\n".join(lines[:20]))
        return {"rebuilt": True, "cards": len(data["cards"]),
                "transitions": len(lines), "why": why}

    def _planroom_timer(self) -> None:
        """The daemon's own rebuild tick."""
        interval = self.planroom.get("timer_sec", DEFAULT_PLANROOM_TIMER_SEC)
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            interval = DEFAULT_PLANROOM_TIMER_SEC
        if interval <= 0:
            return
        while not self._closed:
            # Sleep first: startup already rebuilds nothing in particular, and a
            # daemon that re-derives the whole repo the instant it comes up makes a
            # restart the most expensive thing on the host.
            slept = 0.0
            while slept < interval and not self._closed:
                time.sleep(min(1.0, interval - slept))
                slept += 1.0
            if self._closed:
                return
            try:
                self._planroom_rebuild("timer")
            except Exception:  # noqa: BLE001 — belt and braces; _planroom_rebuild
                # already swallows, and a dead timer thread is a board that silently
                # stops moving.
                pass

    def _start_planroom_timer(self) -> None:
        if not self._planroom_index_path() or self._planroom_thread is not None:
            return
        t = threading.Thread(target=self._planroom_timer, daemon=True)
        self._planroom_thread = t
        t.start()

    # ------------------------------------------------------------- server

    def serve_forever(self) -> None:
        sock_dir = os.path.dirname(self.socket_path)
        if sock_dir and not os.path.isdir(sock_dir):
            os.makedirs(sock_dir, exist_ok=True)
        # Remove a stale socket left by an unclean shutdown (only if it IS a
        # socket).
        try:
            if stat.S_ISSOCK(os.stat(self.socket_path).st_mode):
                os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.socket_path)
        # 0666 on the socket file: connecting is open to all local users because
        # AUTH is by SO_PEERCRED, not file permissions — unknown uids are denied
        # (and audited) inside dispatch().
        os.chmod(self.socket_path, 0o666)
        listener.listen(16)
        # A blocked accept() is not interrupted by close() on Linux, so poll with a
        # short timeout; shutdown() additionally pokes the socket.
        listener.settimeout(1.0)
        self._listener = listener
        # The Plan Room's third rebuild trigger (P4).
        self._start_planroom_timer()
        while not self._closed:
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break  # listener closed by shutdown()
            if self._closed:
                conn.close()
                break
            threading.Thread(target=self._handle_conn, args=(conn,),
                             daemon=True).start()

    def shutdown(self) -> None:
        self._closed = True
        # Wake a pending accept() immediately (best-effort).
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as poke:
                poke.settimeout(0.2)
                poke.connect(self.socket_path)
        except OSError:
            pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def _handle_conn(self, conn: socket.socket) -> None:
        """One connection = one request line = one response line."""
        try:
            conn.settimeout(30)
            # Kernel-asserted peer credentials: (pid, uid, gid).
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REQUEST_BYTES:
                    # WP-H13 F1: audit this rejection like every other.
                    self._audit(f"uid:{uid}" if uid not in self.uid_map
                                else self.uid_map[uid],
                                "(oversize)", None, False, "denied: request too large")
                    self._send(conn, self._err("bad-args", "request too large"))
                    return
            line = buf.split(b"\n", 1)[0].strip()
            if not line:
                return  # connect-and-close probe; nothing to do or audit
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                self._audit(f"uid:{uid}" if uid not in self.uid_map
                            else self.uid_map[uid],
                            "(unparseable)", None, False, "denied: invalid JSON")
                self._send(conn, self._err("bad-args", "request is not valid JSON"))
                return
            if not isinstance(req, dict):
                req = {"verb": None, "args": None}
            resp = self.dispatch(uid, req.get("verb"), req.get("args", {}))
            self._send(conn, resp)
        except Exception:  # noqa: BLE001 — a bad client never kills the daemon
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        try:
            conn.sendall(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Disjorn privileged verb broker")
    parser.add_argument("--config", default=os.environ.get(ENV_CONFIG, DEFAULT_CONFIG_PATH))
    parser.add_argument("--verbs", default=os.environ.get(ENV_VERBS, DEFAULT_VERBS_PATH))
    ns = parser.parse_args(argv)

    config = load_config(ns.config)
    try:
        broker = Broker(config, ns.verbs)
    except ConfigError as exc:
        # BL-D1 and friends: an unsafe config is a REFUSAL TO START, printed loudly
        # and exited non-zero (systemd Restart=on-failure will retry and the failure
        # stays visible in `systemctl status`).
        print(f"disjorn-broker: REFUSING TO START — {exc}", file=sys.stderr)
        return 2

    def _stop(signum: int, _frame: Any) -> None:
        print(f"disjorn-broker: signal {signum}, shutting down", file=sys.stderr)
        broker.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # WP-L4: builds run in transient units OUTSIDE this daemon's cgroup, so a
    # restart no longer kills one in flight — but its reaper died with the old
    # process.
    try:
        adopted = broker.adopt_inflight_builds()
        if adopted:
            print(f"disjorn-broker: re-adopted in-flight builds: "
                  f"{', '.join(adopted)}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"disjorn-broker: WARNING build re-adoption failed: {exc!r}",
              file=sys.stderr)
    try:
        adopted_apps = broker.adopt_inflight_apps()
        if adopted_apps:
            print(f"disjorn-broker: re-adopted in-flight app turns: "
                  f"{', '.join(adopted_apps)}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — same rule as builds above
        print(f"disjorn-broker: WARNING app turn re-adoption failed: {exc!r}",
              file=sys.stderr)

    print(f"disjorn-broker: listening on {broker.socket_path} "
          f"(config={ns.config}, verbs={ns.verbs})", file=sys.stderr)
    broker.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
