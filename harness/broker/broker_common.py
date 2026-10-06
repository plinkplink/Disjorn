"""Shared limits, argument validators and transports for the broker modules."""

from __future__ import annotations

import datetime as _dt
import os
import pwd
import re
import stat
from typing import Any, Callable, Optional


MAX_REQUEST_BYTES = 64 * 1024  # one request line; anything bigger is hostile
# A card slug, anchored.
BOARD_SLUG_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,50}"
                           r"|backlog-\d{1,12}|keyboard-[0-9a-f]{7,40})$")
SUBPROCESS_TIMEOUTS = {  # seconds, per verb
    "restart-disjorn": 60,
    "run-server-tests": 900,
    "classify-diff": 120,
    "changed-files": 60,
    "read-prod-logs": 30,
    "refresh-mirror": 120,
    "spec-status": 60,
    "merge": 300,
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SPEC_STEM_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9][a-z0-9-]{0,50})$")

BUILD_UNIT_PREFIX = "disjorn-build-"
# Unit states that mean "this build is still going".
BUILD_ACTIVE_STATES = frozenset(
    {"active", "activating", "deactivating", "reloading", "refreshing"})
SERVER_IDENTITY = "server"
BUILD_VERB = "build"
BUILD_REFUSED = "build-refused"
# A message seq / channel id / session id: any positive 32-bit row key.
MAX_SEQ = 2 ** 31 - 1
# Mirrors server/app/privacy.py BOT_HIDDEN_FLAGS; the two must not drift.
BOT_HIDDEN_FLAGS = ("secret", "off_the_record")
# Wall-clock cap for a woken session, in seconds.
DEFAULT_WAKE_SESSION_CAP_SEC = 5400
# How long after the cap a wake is still considered in flight.
DEFAULT_WAKE_GRACE_SEC = 600


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


def read_peer_cgroup(pid: int) -> str:
    """`/proc/<pid>/cgroup` for a socket peer; empty when the peer is already gone."""
    try:
        with open(f"/proc/{int(pid)}/cgroup", "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, ValueError):
        return ""


def hidden_from_bots(privacy_flags: Any) -> bool:
    """The rule in server/app/privacy.py, restated where no server import exists."""
    if not isinstance(privacy_flags, dict) or not privacy_flags:
        return False
    return any(bool(privacy_flags.get(f)) for f in BOT_HIDDEN_FLAGS)


class VerbError(Exception):
    """A verb failed or a request was rejected. code -> PROTOCOL.md error codes."""

    def __init__(self, code: str, message: str,
                 status: Optional[int] = None,
                 reason: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        # Which rule said no (merge, apply-posted-write); on the wire beside
        # code and message.
        self.reason = reason


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


def _split_diff_range(rng: str) -> tuple[str, str]:
    """Both sides of `A..B` / `A...B`; a bare rev names no pair and is refused."""
    sep = "..." if "..." in rng else ".."
    sides = rng.split(sep)
    if len(sides) != 2 or not all(sides):
        raise _bad("range must be A..B or A...B")
    return sides[0], sides[1]


def _safe_path(path: str) -> str:
    """A control character in a filename could forge a line in a reviewer's
    context, so such a path is handed back quoted rather than raw."""
    if any(ch < " " or ch == "\x7f" for ch in path):
        return repr(path)
    return path


def _parse_numstat(out: str) -> dict[str, tuple[Optional[int], Optional[int]]]:
    """`git diff --numstat -z`: a rename's record ends in an empty path field
    and the old and new names follow as two more NUL-separated fields; a binary
    file counts `-` on both sides."""
    fields = out.split("\0")
    counts: dict[str, tuple[Optional[int], Optional[int]]] = {}
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if not record:
            continue
        added, removed, path = record.split("\t", 2)
        if not path:
            path = fields[i + 1]
            i += 2
        counts[path] = (None if added == "-" else int(added),
                        None if removed == "-" else int(removed))
    return counts


def _parse_name_status(out: str) -> list[tuple[str, str, Optional[str]]]:
    """`git diff --name-status -z` as (status letter, path, old path or None);
    R and C carry a similarity score after the letter and two path fields."""
    fields = out.split("\0")
    rows: list[tuple[str, str, Optional[str]]] = []
    i = 0
    while i < len(fields):
        code = fields[i]
        i += 1
        if not code:
            continue
        if code[0] in ("R", "C"):
            rows.append((code[0], fields[i + 1], fields[i]))
            i += 2
        else:
            rows.append((code[0], fields[i], None))
            i += 1
    return rows


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


def _sdk_channel_transport(disjorn_cfg: dict, channel_id: int,
                           body: str) -> dict:
    """POST body to ONE named channel, rather than to #custodian."""
    import asyncio

    from disjorn_sdk import DisjornClient  # deferred import: not needed in tests

    url = disjorn_cfg["url"]
    with open(disjorn_cfg["api_key_path"], "r", encoding="utf-8") as fh:
        api_key = fh.read().strip()

    async def _post() -> dict:
        client = DisjornClient(url, api_key=api_key)
        try:
            msg = await client.send(int(channel_id), body)
        finally:
            await client.aclose()
        return {"seq": msg.get("seq"), "message_id": msg.get("id")}

    return asyncio.run(_post())


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


_BUILD_CALLER_RE = re.compile(r"^res-([a-z][a-z0-9]{0,30})$")
