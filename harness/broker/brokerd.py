#!/usr/bin/env python3
"""disjorn-broker — the privileged verb gateway for residents (WP-H3).

Residents (res-claudette, res-gable) live in rootless containers with no sudo
and a walled network. The ONLY way anything privileged happens on their behalf
is through this daemon: a unix-socket server whose caller identity comes from
SO_PEERCRED (kernel-asserted uid), never from anything the caller says.

Governance rules encoded here (AGENTHOOD.md / HARNESS-PLAN.md WP-H3):

* Kill switches: every verb is per-resident toggleable in verbs.toml, which is
  plink-owned and lives OUTSIDE both containers. Toggles default to OFF and
  verbs.toml is re-read on every request, so flipping a switch needs no broker
  restart.
* Chat is data, never authorization: nothing in a request body can widen what
  a caller may do. Identity = uid via SO_PEERCRED; permission = verbs.toml.
* No self-restart: there is deliberately NO `restart-self` verb, and no verb
  whose argv a caller can redirect at the broker or a resident's own process.
* No free-form shell, ever: every subprocess runs a fixed argv list
  (config-supplied list + individually validated scalar args appended by the
  handler). The shell-enabled subprocess mode is never used in this file.
* Total audit: every call — allowed, denied, or malformed — appends exactly
  one JSON line {ts, resident, verb, args, allowed, result_summary} to the
  audit log (a verb may add extra FACT fields; it can never overwrite those).
* Unsafe config = refuse to start: invariants that a verb's authorization
  rests on are asserted at CONSTRUCTION and raise ConfigError, which main()
  reports loudly and exits non-zero on. The one today is BL-D1 — start-build's
  specs_dir must be provably resident-unwritable. There is no degraded mode:
  a gateway that quietly drops one guarantee is worse than one that is down.

Config: /etc/disjorn-broker/broker.toml + verbs.toml (templates alongside this
file). Paths overridable for tests via DISJORN_BROKER_CONFIG /
DISJORN_BROKER_VERBS or --config/--verbs.

Runs as plink (not root) under systemd. There are exactly TWO privileged escape
hatches, both narrow sudoers rules and both listed here so a third is a visible
act rather than a habit:
  * harness/keyboard/90-disjorn-broker.sudoers — `sudo -n systemctl restart
    disjorn`, nothing else (WP-H3, restart-disjorn).
  * harness/keyboard/91-disjorn-build.sudoers — `sudo -n disjorn-build-launch
    run|stop <resident> <slug>`, nothing else (WP-L4, start-build). That helper
    (harness/broker/disjorn-build-launch) is the only thing that runs as root;
    what it starts runs as the RESIDENT, in a transient systemd unit. It ships
    UNINSTALLED, like the verb it serves.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pwd
import re
import signal
import socket
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
# shipped broker.toml template uses /run/disjorn-broker/broker.sock instead so
# the daemon can run unprivileged under systemd RuntimeDirectory=.

MAX_REQUEST_BYTES = 64 * 1024  # one request line; anything bigger is hostile
MAX_PROPOSAL_CHARS = 4000
MAX_LOG_LINES = 500
MAX_AUDIT_ENTRIES = 500
MAX_GREP_CHARS = 200
MAX_GATES_JSON = 8192
SUBPROCESS_TIMEOUTS = {  # seconds, per verb
    "restart-disjorn": 60,
    "run-server-tests": 900,
    "classify-diff": 120,
    "read-prod-logs": 30,
    "refresh-mirror": 120,
}

# start-build (WP-L4): the detached build is NOT a synchronous _run() call, so
# its wall-clock cap lives in config ([start_build].timeout_sec), not the dict
# above; this is only the fallback when config omits it. Longer than the 300s
# summon on purpose — a build is a whole feature, not a chat turn.
START_BUILD_DEFAULT_TIMEOUT = 3600
# Ratified default (BUILD-LOOP.md): builds are CAPPED by default (2/day), unlike
# the WP-H12 action budget which ships OFF. plink tunes at staging time.
DEFAULT_DAILY_BUILD_CAP = 2
MAX_SPEC_BYTES = 64 * 1024  # a spec is a short markdown doc; bigger is hostile
# BL-D2: the detached build's stdout/stderr go to temp FILES (bounded on disk),
# never to a pipe the privileged broker must drain into RAM. Only this much of
# each is ever read back for the done/failed narration. 64 KiB is generous for
# a final JSON report and a stack trace, and is a hard ceiling on how much
# resident-influenced text can enter the broker's address space per build.
MAX_BUILD_LOG_TAIL = 64 * 1024

_RANGE_RE = re.compile(r"^[A-Za-z0-9._~^/{}-]{1,200}$")  # git rev / range; no
# whitespace, no leading dash (checked separately) — can never be read as a flag
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A spec slug also names the build branch (loop/<slug>) and the build container
# (disjorn-build-<slug>, run-build.sh) and rides argv as a positional, so it is
# held to a strict branch/argv-safe kebab charset — it can never be read as a
# flag or a path segment.
#
# BL-D4: the slug KEEPS the spec's `YYYY-MM-DD-` prefix. It used to be stripped,
# so `2026-07-21-gif-picker.md` and `2026-09-02-gif-picker.md` both derived
# `loop/gif-picker` + `disjorn-build-gif-picker` — concurrent runs collided on
# podman `--name`, sequential runs clobbered each other's branch. The date is a
# DETERMINISTIC, human-readable disambiguator already present in the filename
# (chosen over a counter/hash: the branch name now equals the spec's basename
# 1:1, so any branch traces back to exactly one spec file with no lookup), and
# it is the same string plink already types. The same-spec-twice case (same
# date, same name) is closed separately by the in-flight slug guard in
# _reserve_build.
_SPEC_STEM_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9][a-z0-9-]{0,50})$")

# WP-L4 open fork (KEYBOARD-NEXT 6b): a build is launched as a TRANSIENT SYSTEM
# SERVICE under the resident's own uid, via `sudo -n disjorn-build-launch run
# <resident> <slug> …` (harness/broker/disjorn-build-launch). The unit name is a
# pure function of the slug, and BOTH sides compute it the same way — the helper
# so it can pin `--unit=`, the broker so it can stop, poll and re-adopt a build
# it did not launch this process. A test asserts the two agree.
BUILD_UNIT_PREFIX = "disjorn-build-"
# Unit states that mean "this build is still going". Anything else (inactive,
# failed, or the unit having been --collect'ed out of existence) is terminal.
BUILD_ACTIVE_STATES = frozenset(
    {"active", "activating", "deactivating", "reloading", "refreshing"})
# One JSON sidecar per in-flight build, written next to its output spool BEFORE
# the launch. It is what makes a build survivable: after a broker restart the
# reaper thread is gone, but the unit is not (it lives outside the broker's
# cgroup), so the new process re-reads these and re-adopts.
BUILD_SIDECAR_SUFFIX = ".build.json"
BUILD_SIDECAR_SCHEMA = 1


def build_unit_name(slug: str) -> str:
    """`2026-07-21-gif-picker` -> `disjorn-build-2026-07-21-gif-picker.service`.

    Deterministic on purpose: the unit is greppable from the slug alone
    (`systemctl status disjorn-build-<slug>`), it maps 1:1 to the branch and the
    podman container name, and a duplicate launch COLLIDES LOUDLY in systemd
    ("Unit … was already loaded") instead of racing the branch — a kernel-side
    backstop under the broker's own in-flight slug claim (BL-D4).

    Validates the slug exactly as slug_from_spec_filename does — shape AND a
    real calendar date — because this is also the function that vets a slug
    arriving from a sidecar written by a PREVIOUS broker process, and because
    the privileged helper applies the same test at the sudo boundary. Two
    validators guarding one name must agree, or one of them is decoration."""
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

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _bad(msg: str) -> VerbError:
    return VerbError("bad-args", msg)


class ConfigError(Exception):
    """Broker configuration is unsafe. Raised at CONSTRUCTION time only, and
    never caught inside the daemon: the broker refuses to start. There is no
    degraded mode — a gateway that quietly drops one guarantee is worse than
    one that is visibly down (AGENTHOOD: fail loud, never fail over)."""


# --------------------------------------------------------------------------
# BL-D1 — the start-build authorization surface, enforced instead of commented.
#
# THE INVARIANT.  The start-build confirm gate ("## Status: confirmed" + a
# filled "## Confirm record") is a presence check on TEXT. Text inside SPECS/
# is only trustworthy because SPECS/ is the plink-gated read-only mirror
# (/srv/disjorn-ro/SPECS) that no resident can write. Point `specs_dir` at
# anything a resident can write and the gate silently inverts: a resident
# writes its own spec, forges "Confirmed by: plink", and self-authorizes a
# build within its 2/day budget. Chat is data, never authorization — and a
# file a resident can write IS chat.
#
# So the invariant is enforced mechanically, at construction (broker startup),
# not per-request: a bad specs_dir is config drift plink must see immediately,
# not a surprise on the first build. Two independent rules, both fail-loud:
#
#   RULE 1 (structural).  realpath(specs_dir) must not sit inside any resident
#     volume: `/home/<resident>` for every resident name in [uids]/[residents]
#     (the run-resident.sh / run-build.sh home volume is mounted READ-WRITE by
#     construction), any explicitly declared [residents.<r>].writable_roots,
#     or any [residents.<r>.path_map] host target that resolves inside one of
#     those. realpath() means a symlink planted anywhere in the chain cannot
#     smuggle the directory into a resident volume.
#
#     NB: bare path_map membership is deliberately NOT fatal. The shipped
#     config maps "/opt/disjorn" -> "/srv/disjorn-ro" and puts specs_dir at
#     /srv/disjorn-ro/SPECS: the mirror is BOTH the residents' view of the repo
#     AND the authoritative specs dir, on purpose. What makes it safe is that
#     it is read-only to them, which is exactly what RULE 2 measures.
#
#   RULE 2 (permissions).  specs_dir must exist, be a directory, and neither it
#     nor ANY parent component up to / may be writable by a resident uid, by a
#     group a resident belongs to, or by "other". A writable parent is as fatal
#     as a writable leaf — whoever can write /srv/disjorn-ro can replace SPECS
#     wholesale. One exception, only for parents: a sticky (S_ISVTX) directory
#     such as /tmp, where the kernel forbids renaming or deleting entries you
#     do not own, so a resident cannot swap out the next path component. The
#     leaf never gets that exemption — creating a NEW file in a sticky dir is
#     allowed, and a new .md in SPECS/ is the whole attack.
#
# CARVE-OUT (deliberate, and the only one): a resident uid equal to the
# broker's own euid is skipped when computing "writable by a resident". Such a
# caller is not contained by anything — it already runs as the broker, can read
# broker.toml, rewrite verbs.toml's target, and kill the daemon. Treating its
# write access as an escalation would be theatre, and it is what lets the test
# harness map the running uid to a resident (SO_PEERCRED needs a real uid)
# without disabling this guard for the paths that matter.
# --------------------------------------------------------------------------

def _resident_gids(uid: int) -> set[int]:
    """Every gid a uid belongs to (primary + supplementary). Empty if the uid
    has no passwd entry — the uid check still applies; only the group check
    degrades. That is a known, documented gap rather than a refusal, because
    residents always exist on the host (01-users.sh) while test/staging uid
    maps legitimately name uids that do not."""
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
    """True if `path` IS `root` or sits underneath it. Both must already be
    realpath()ed; the trailing-separator form stops /home/res-gable-evil from
    matching /home/res-gable."""
    if path == root:
        return True
    return path.startswith(root.rstrip("/") + "/")


def _path_components(path: str) -> list[str]:
    """`/a/b/c` -> ['/a/b/c', '/a/b', '/a', '/'] — the leaf first, then every
    parent up to the root, so a caller can stat the whole chain."""
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
    ConfigError naming the offending path. Returns realpath(specs_dir), which
    the caller should use from then on — the checked path and the used path
    must be the same string.

    Pure with respect to broker state: it takes the uid map, the residents
    table and (injectably) the uid->gids resolver, so every adversarial case
    is testable without creating real users."""
    if broker_uid is None:
        broker_uid = os.geteuid()
    real = os.path.realpath(specs_dir)

    # Resident identities. Names come from BOTH tables so a half-configured
    # deployment still contributes its home root (fail closed on omission).
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
    # path_map host targets count only when they land inside one of the roots
    # above (see the NB in the block comment: /srv/disjorn-ro is a path_map
    # target AND the intended specs dir).
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
                f"start_build.specs_dir is resident-writable: {specs_dir!r} "
                f"resolves to {real!r}, which is inside {why}. The confirm "
                f"gate is only meaningful when SPECS/ is the plink-gated "
                f"read-only mirror; point specs_dir there (e.g. "
                f"/srv/disjorn-ro/SPECS).")

    # ---- RULE 2: not writable by any resident, leaf or parent -------------
    if not os.path.isdir(real):
        raise ConfigError(
            f"start_build.specs_dir does not exist or is not a directory: "
            f"{specs_dir!r} (resolved {real!r}). Refusing to start rather than "
            f"guess — an absent specs dir cannot be verified unwritable.")
    for i, component in enumerate(_path_components(real)):
        is_leaf = i == 0
        try:
            st = os.stat(component)
        except OSError as exc:
            raise ConfigError(
                f"start_build.specs_dir path component {component!r} cannot be "
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
                f"start_build.specs_dir is resident-writable: path component "
                f"{component!r} (of {real!r}) is {why}. A resident that can "
                f"write any component of SPECS/ can forge its own confirm "
                f"record and self-authorize a build. Refusing to start.")
    return real


# --------------------------------------------------------------------------
# Argument validation.  Every verb has an explicit schema; unknown keys are
# rejected; every value is type- and range-checked before a handler sees it.
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
# Default file-proposal transport: post to #custodian via the Disjorn SDK as
# the broker's own bot identity.  Kept behind a callable so tests stub it.
# --------------------------------------------------------------------------

def _sdk_transport(disjorn_cfg: dict, body: str) -> dict:
    """POST body to the configured custodian channel. Returns {seq, message_id}."""
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
# start-build (WP-L4): spec parsing, slug/branch derivation, the build-session
# prompt, and #custodian narration. Pure functions — no I/O, no broker state —
# so the confirm gate, the slug rules, and every narration shape are unit-
# testable in isolation, exactly like the argv validators above.
# --------------------------------------------------------------------------

def _clean_field(value: str) -> Optional[str]:
    """A spec field value, or None if it is blank or still the TEMPLATE.md
    placeholder (angle-bracketed `<...>`). This is how "the confirm record is
    unfilled" is detected mechanically — a spec left with `<username>` in the
    box has no confirm record, whatever it looks like at a glance."""
    v = value.strip()
    if not v or v in {"-", "_"}:
        return None
    if v.startswith("<") and v.endswith(">"):
        return None
    return v


def parse_spec_status(text: str) -> Optional[str]:
    """The status token under `## Status` (e.g. 'confirmed'), lowercased, or
    None if the section is absent. Backticks and HTML comments are ignored —
    TEMPLATE.md writes the token as `` `confirmed` `` trailed by a comment."""
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


def parse_confirm_record(text: str) -> dict:
    """`{confirmed_by, seq}` from the `## Confirm record` section. A field that
    is blank or still the `<...>` placeholder comes back None — mechanically,
    that IS "no confirm record". `seq` is the witnessing #custodian sequence as
    an int (or None). Chat is data: the broker verifies this record, it never
    trusts a caller's word that a build was confirmed."""
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
        m = re.match(r"\s*-\s*\*\*Confirmed by\*\*:\s*(.*)$", line)
        if m:
            out["confirmed_by"] = _clean_field(m.group(1))
        m = re.match(r"\s*-\s*\*\*#custodian seq\*\*:\s*(.*)$", line)
        if m:
            raw = _clean_field(m.group(1))
            if raw is not None:
                digits = re.search(r"\d+", raw)
                out["seq"] = int(digits.group()) if digits else None
    return out


def slug_from_spec_filename(filename: str) -> str:
    """`SPECS/YYYY-MM-DD-<name>.md` -> `YYYY-MM-DD-<name>` (branch =
    loop/<slug>). The date prefix is REQUIRED and KEPT (BL-D4: it is the
    collision disambiguator — see _SPEC_STEM_RE), the date must be a real
    calendar date, and the remainder must be a strict kebab name. Anything else
    is bad-args, because this string ends up as a git branch, a podman
    container name, and an argv positional."""
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
    """The instruction preamble + the committed spec, fed to the build session
    on STDIN. ALL of it is data on stdin — argv stays config-only (launcher
    doctrine): only the mechanically-validated slug/branch and fixed broker
    text vary here, and the branch/no-merge/no-push rules are stated where the
    session actually reads them."""
    return (
        f"You are a Disjorn build session. Build exactly what the spec below "
        f"describes, on a NEW git branch named `{branch}` in your worktree.\n"
        f"Hard rules (non-negotiable):\n"
        f"- Do NOT merge. Do NOT push. Do NOT touch the production service.\n"
        f"- Everything you do lands on `{branch}` and waits there for a human.\n"
        f"- Narrate STATE TRANSITIONS ONLY to #custodian (channel 4): each\n"
        f"  checkpoint you choose to mark. No heartbeats, no timers — go quiet\n"
        f"  between transitions, and fail loud if you get stuck.\n"
        f"- When done, print a final JSON object on stdout with keys "
        f'"files" (paths touched), "tests" (what you ran + result), '
        f'"diff" (one-line summary), "branch".\n\n'
        f"--- SPEC ({slug}) ---\n{spec_text}"
    )


def _parse_build_report(stdout: str) -> dict:
    """Best-effort structured report from the build session's stdout for the
    'done' line. The session is asked to end with a JSON object
    {files, tests, diff, branch}; we surface those and degrade to 'n/a' (or a
    text tail) if it didn't. Tier is intentionally NOT computed here — see
    format_build_done: classify-diff is a separate verb, not coupled in.

    BL-D2: the input is now the bounded TAIL of the build's stdout file, not
    the whole stream, so it may begin mid-line. Hence the second attempt on the
    last non-blank line — the report is the last thing printed, and a truncated
    head must not cost us the report."""
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
                try:
                    parsed = json.loads(v)
                except json.JSONDecodeError:
                    continue
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


def format_build_started(*, slug: str, branch: str, confirmed_by: str,
                         seq: int, eta_sec: int) -> str:
    """The 'started' state-transition line. Names the spec, the branch, who
    confirmed it + the witnessing seq, and an ETA GUESS (the wall-clock cap, a
    ceiling not a promise). Plain text, greppable, no emoji — same house idiom
    as the summon summaries."""
    eta_min = max(1, eta_sec // 60)
    return (f"build started | {slug} -> {branch} | "
            f"confirmed by {confirmed_by} (#custodian seq {seq}) | "
            f"ETA <= {eta_min}m (guess) | no merge, no push — lands on the branch")


def format_build_done(*, slug: str, branch: str, files: str, tests: str,
                      diff: str, tier: str = "pending") -> str:
    """The 'done' state-transition line: files touched, tests run/result, a
    one-line diff summary, the branch, and the advisory tier. Tier is 'pending'
    by default — the reaper does not invoke classify-diff (a separate verb,
    ships OFF); a human runs it on the branch. Nothing merged, ever."""
    return (f"build done | {slug} -> {branch} | tier {tier} | "
            f"files: {files} | tests: {tests} | diff: {diff} | "
            f"on the branch for review — nothing merged")


def format_build_failed(*, slug: str, branch: str, reason: str) -> str:
    """The 'failed' state-transition line — LOUD. A stalled build goes quiet
    then lands here (never a heartbeat); the branch keeps whatever exists and a
    human is told to look."""
    return (f"BUILD FAILED | {slug} -> {branch} | {reason} | "
            f"the branch holds what there is — a human should look")


# --------------------------------------------------------------------------
# The broker.
# --------------------------------------------------------------------------

class Broker:
    """Unix-socket verb broker. Construct with parsed broker.toml + a path to
    verbs.toml (re-read per request — that's the kill-switch property)."""

    # How often an ADOPTED build's unit is polled for its terminal state. Only
    # reached after a broker restart with a build in flight (a rare event on a
    # rare verb), so it is deliberately lazy — the cost of noticing a minute
    # late is one late #custodian line. Tests turn it down.
    BUILD_POLL_SEC = 5.0

    def __init__(
        self,
        config: dict,
        verbs_path: str,
        *,
        transport: Optional[Callable[[dict, str], dict]] = None,
        build_spawn: Optional[Callable[[list[str]], Any]] = None,
    ) -> None:
        self.config = config
        self.verbs_path = verbs_path
        self.transport = transport or _sdk_transport
        # How a detached build session is launched. Injected in tests (mock the
        # exec); prod uses _default_build_spawn (a detached, un-waited Popen).
        self._build_spawn = build_spawn or self._default_build_spawn
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
        # Daily per-resident action budget (WP-H12). Loaded at construction;
        # a cap change needs a broker restart (unlike verbs.toml kill switches,
        # which are re-read live). Default: no cap == OFF. Instrument first.
        self.budgets: dict[str, Any] = config.get("budgets", {})
        # start-build (WP-L4) config: the detached build-session launch contract
        # (command + session_argv + model pin), the SPECS/ dir the confirm gate
        # reads, the wall-clock cap, and the per-day build budget.
        self.start_build: dict[str, Any] = config.get("start_build", {})
        # BL-D1: the confirm gate's REAL authorization is that specs_dir is
        # resident-unwritable. Verified HERE, once, at startup — a violation
        # raises ConfigError and main() exits non-zero, so the broker never
        # comes up with a forgeable confirm gate. A config with no [start_build]
        # section at all is not checked: start-build then fails closed at
        # request time (_specs_dir raises internal), so there is nothing to
        # forge. Presence of the section means someone intends to run builds,
        # and then specs_dir is mandatory and audited.
        self.specs_dir_real: Optional[str] = None
        if self.start_build:
            specs_dir = self.start_build.get("specs_dir")
            if not isinstance(specs_dir, str) or not specs_dir:
                raise ConfigError(
                    "[start_build] is configured but start_build.specs_dir is "
                    "missing; refusing to start (the confirm gate has no "
                    "trustworthy source)")
            self.specs_dir_real = assert_specs_dir_resident_unwritable(
                specs_dir, uid_map=self.uid_map, residents=self.residents)
        self._audit_lock = threading.Lock()
        # Build-budget lock (H13-D4): count-with-reservation is held under this,
        # so two concurrent start-builds can NEVER both slip past the cap — the
        # check-then-act race the red-team flagged is closed here.
        self._build_lock = threading.Lock()
        # Action-budget lock (H13-D4, extended to EVERY numeric budget): same
        # count-with-reservation discipline as builds. The daily action cap used
        # to be a check-then-act against the audit file, so N concurrent
        # dispatches all read the same pre-cap count and all ran.
        self._action_lock = threading.Lock()
        # Per-resident build reservations for the day: resident -> (utc_date,
        # count). Seeded lazily from the audit log per day, then authoritative
        # in memory (never re-read, so in-flight builds are never double-counted).
        self._builds: dict[str, tuple[Optional[str], int]] = {}
        # Same shape for the action budget: resident -> (utc_date, count).
        self._actions: dict[str, tuple[Optional[str], int]] = {}
        # BL-D4: slugs of builds currently in flight. Two builds of the SAME
        # spec would collide on podman `--name disjorn-build-<slug>` and on the
        # loop/<slug> branch; the dated slug separates different specs, this
        # separates the same spec launched twice. Guarded by _build_lock.
        self._active_builds: set[str] = set()
        # Detached build reaper threads, kept ONLY so tests can join them;
        # production never waits on a build — detachment is the whole point.
        self._build_threads: list[threading.Thread] = []
        self._listener: Optional[socket.socket] = None
        self._closed = False

        # The verb table.  Adding a verb here is a deliberate act; there is no
        # dynamic registration and — enforced by test — no "restart-self".
        # Handlers return (result, audit_summary) or (result, audit_summary,
        # audit_extra) — see dispatch().
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
        }

    # ------------------------------------------------------------- audit

    def _audit(self, resident: str, verb: str, args: Any, allowed: bool,
               result_summary: str, extra: Optional[dict] = None) -> None:
        """One JSON line per call. `extra` adds verb-specific FACTS that later
        readers must be able to trust (BL-D3: `build_started`), and can never
        overwrite the six core keys — a verb cannot rewrite its own identity,
        caller, or allowed-ness in the trail."""
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
        """Per-resident daily action cap from `[budgets]`, or None (off).
        `[budgets.<resident>].daily_action_cap` wins; else
        `[budgets].default_daily_action_cap`; else None."""
        per = self.budgets.get(resident)
        if isinstance(per, dict) and isinstance(per.get("daily_action_cap"), int):
            return per["daily_action_cap"]
        default = self.budgets.get("default_daily_action_cap")
        return default if isinstance(default, int) else None

    def _count_today_allowed(self, resident: str) -> int:
        """How many ALLOWED actions this resident has today (UTC), read from
        the audit log — the same source the metrics producer aggregates, so
        the count is authoritative and restart-proof. Denials never count."""
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
        """Race-safe action-budget check + reservation (H13-D4). Identical
        discipline to _reserve_build: seed the day's count from the audit log
        once, then hold count AND reserve under ONE lock, so N concurrent
        dispatches can never all read the same pre-cap count and all proceed.
        Raises over-budget at/over the cap (a denial: the verb never runs)."""
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
        consume budget — a resident cannot exhaust its own cap by being refused
        (the WP-H12 contract, preserved verbatim under reservation)."""
        with self._action_lock:
            date, count = self._actions.get(resident, (None, 0))
            if count > 0:
                self._actions[resident] = (date, count - 1)

    # -------------------------------------------------------- build budget

    def _daily_build_cap(self, resident: str) -> Optional[int]:
        """Per-day build cap for a resident.
        `[start_build.per_resident.<r>].daily_build_cap` wins; else
        `[start_build].daily_build_cap`; else the ratified default of 2. Builds
        are capped by DEFAULT (BUILD-LOOP.md), unlike the WP-H12 action budget:
        the blast radius of an autonomous build is a whole branch of tokens."""
        per = self.start_build.get("per_resident")
        if isinstance(per, dict):
            r = per.get(resident)
            if isinstance(r, dict) and isinstance(r.get("daily_build_cap"), int):
                return r["daily_build_cap"]
        cap = self.start_build.get("daily_build_cap", DEFAULT_DAILY_BUILD_CAP)
        return cap if isinstance(cap, int) else DEFAULT_DAILY_BUILD_CAP

    def _count_builds_today(self, resident: str, today: str) -> int:
        """Builds this resident GENUINELY STARTED today (UTC). Used ONLY to
        seed the in-memory reservation counter once per day; after seeding the
        counter is authoritative, so a build launched this process (already
        reserved in memory, not yet reflected here until dispatch writes its
        line) is never counted twice.

        BL-D3: the marker is the audit record's `build_started: true` flag, not
        `allowed: true`. A spawn OSError is an authorized-but-failed call — it
        audits allowed=True (correctly: the verb ran) and refunds its in-memory
        slot, so counting allowed=True lines made a build that NEVER STARTED
        consume a slot after a broker restart, with memory and disk disagreeing.
        Only _verb_start_build's success path emits the marker, so
        never-started and ran-then-failed are now distinguishable on disk.
        (Consequence, deliberate: audit lines written before this field existed
        do not reseed. start-build has never run outside tests — it ships OFF —
        so there are none, and undercounting a soft budget fails safe anyway.)"""
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
        """Race-safe build-budget check + reservation (H13-D4: count-with-
        reservation under a lock, NEVER check-then-act on the audit file), plus
        the BL-D4 in-flight uniqueness claim on the slug.
        Under one lock: refuse a slug already building, seed the day's count
        from the audit log if unseen, refuse at/over the cap, else reserve a
        slot + claim the slug and return (used_after, cap). Because the lock
        spans count AND reserve, concurrent start-builds can never both pass a
        cap of N, and two builds can never share a branch or container name."""
        cap = self._daily_build_cap(resident)
        with self._build_lock:
            if slug in self._active_builds:
                # bad-args (a denial, so it burns no budget and audits
                # allowed=False): the caller can fix it by waiting or by
                # writing a distinct spec. Loud rather than silently racing
                # podman --name / the loop/<slug> branch.
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
        """Refund a reservation AND drop the slug claim when the launch itself
        never started (a build that ran and then failed keeps its slot — it
        burned the attempt; see _finish_build)."""
        with self._build_lock:
            date, count = self._builds.get(resident, (None, 0))
            if count > 0:
                self._builds[resident] = (date, count - 1)
            self._active_builds.discard(slug)

    def _finish_build(self, slug: str) -> None:
        """Release the BL-D4 slug claim when a started build reaches a terminal
        state. The BUDGET slot is deliberately NOT refunded — the build ran."""
        with self._build_lock:
            self._active_builds.discard(slug)

    def join_builds(self, timeout: float = 5.0) -> None:
        """Join detached build reaper threads — TEST convenience only.
        Production never waits on a build (detachment is the whole point)."""
        for t in list(self._build_threads):
            t.join(timeout)

    # --------------------------------------------------------------- core

    def dispatch(self, uid: int, verb: Any, args: Any) -> dict:
        """Authorize + execute one request. Always writes exactly one audit line."""
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

        # Daily per-resident action budget (WP-H12). Default OFF: with no cap
        # configured this never denies. The day's count is seeded from the audit
        # log (so it survives a broker restart) and then reserved in memory
        # under a lock — H13-D4: reading the count and acting on it must be one
        # atomic step, or N concurrent dispatches all see the same pre-cap count
        # and all run. The (cap+1)-th action is denied and audited like any
        # other denial. Additive and permissive by default — instrument first,
        # tune from observed data (AGENTHOOD budget rule), never from imagined
        # abuse.
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
            # Verbs return (result, summary) or (result, summary, audit_extra);
            # only start-build uses the third slot today (BL-D3's `build_started`
            # marker), so no other handler had to change.
            result, summary, extra = out if len(out) == 3 else (*out, None)
        except VerbError as exc:
            # A denial (the verb never ran) audits allowed=False; an authorized
            # run that failed audits allowed=True. bad-args is a denial; so is a
            # handler-raised over-budget (e.g. the WP-L4 build budget, refused
            # before any launch) — neither reached execution. A denial also
            # REFUNDS the action reservation: denials must not consume budget.
            allowed = exc.code not in ("bad-args", "over-budget")
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
        # `sudo -n`: never prompts; works only because of the single sudoers
        # line installed by harness/keyboard/04-broker.sh.
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

    def _verb_refresh_mirror(self, resident: str, args: dict) -> tuple[dict, str]:
        """Fast-forward the shared read-only repo mirror to the canonical
        repo's main. The mirror is the ONLY view of the repo residents have
        (bind-mounted RO into each container), and nothing else ever fetches
        into it — host commits don't cross the wall until this runs. Zero
        caller args; every argv is fixed config, so a resident can refresh
        the mirror but can never aim git anywhere else. `--ff-only` on the
        update: a diverged mirror fails loudly and stays plink's to resolve."""
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
        head = _head()
        return ({"head": head, "before": before, "updated": head != before},
                f"mirror at {head}" + ("" if head == before else f" (was {before})"))

    # ------------------------------------------------------------ start-build

    def _specs_dir(self) -> str:
        """The SPECS/ dir the confirm gate reads. Prefers the realpath VERIFIED
        at construction (BL-D1) over the raw config string, so the directory the
        gate reads is byte-for-byte the one proven resident-unwritable — a
        later mutation of self.start_build (tests, a future reload path) can
        never move the gate to an unchecked path."""
        if self.specs_dir_real:
            return self.specs_dir_real
        d = self.start_build.get("specs_dir")
        if not d or not isinstance(d, str):
            raise VerbError("internal", "start_build.specs_dir is not configured")
        return d

    def _resolve_spec_path(self, spec: str) -> str:
        """Map caller input to a real spec file, CONFINED to the configured
        SPECS/ dir. realpath() resolves BOTH `..` traversal and symlink escape,
        then we require the resolved file to sit DIRECTLY in SPECS/ (the flat
        one-file-per-spec layout) and end in .md. A caller can never point the
        builder outside SPECS/ — not with `..`, not through a planted symlink,
        not with an absolute path. The path is caller input; the confinement is
        the broker's, verified mechanically."""
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
        """Read + validate the spec at `path`: status must be 'confirmed' and
        the confirm record must be filled (Confirmed by + #custodian seq).
        No confirm record -> refuse, fail-loud. Returns the fields the launch
        and narration need. The verbs.toml toggle authorizes the CLASS (this
        resident may build); THIS record selects the instance and the broker
        verifies it — chat is data, never authorization."""
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

    def _build_argv(self, slug: str) -> list[str]:
        """The detached build command — a PURE function of config + the
        validated slug. Mirrors the summon launcher's contract
        (launcher.build_argv):
            [*command, resident, slug, *session_argv, "--model", model]
        Only fixed config and the mechanically-validated kebab slug (branch/
        argv-safe) reach argv; the spec — the chat-derived design — rides on
        STDIN. The model pin is WP-L5's idiom: appended as `--model <id>`,
        forwarded by run-build.sh through the bash wrapper's "$@", with NO
        fallback (a blank pin is config drift and fails loud here, never
        silently rides the account default)."""
        command = self.start_build.get("command", [])
        if (not isinstance(command, list) or not command
                or not all(isinstance(a, str) for a in command)):
            raise VerbError("internal",
                            "start_build.command must be a non-empty list of strings")
        resident_arg = str(self.start_build.get("resident", "gable"))
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
        """Launch the build DETACHED so it outlives this request.
        `start_new_session=True` puts it in its OWN session/process group — a
        broker signal to its own foreground group never reaches it — and the
        broker does NOT wait: a daemon reaper feeds the spec on stdin, holds the
        wall-clock cap, and narrates the terminal transition. Fixed argv, shell
        NEVER involved (same discipline as _run).

        BL-D2: stdout/stderr are FILES supplied by the caller, not pipes. A
        build session is resident-influenced and runs up to timeout_sec (3600s
        default); piping it meant the privileged broker buffered the whole
        stream in RAM (measured: 180MB of stdout -> 540MB broker RSS), so one
        chatty build could OOM the verb gateway for EVERY resident. Only stdin
        stays a pipe — that is how the spec is delivered."""
        return subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    # -------------------------------------------------- build output (BL-D2)

    def _build_log_dir(self) -> str:
        """Where the detached build's stdout/stderr files live.

        Order: `[broker].build_log_dir`, else a `build-logs/` subdirectory of
        the audit log's directory (the unit's LogsDirectory=, plink-owned 0750
        and DISK-backed), else the process temp dir as a last resort.

        NOT the temp dir by default, deliberately: /tmp is tmpfs on this host,
        so spooling a flooding build there would put the bytes back in RAM —
        the very thing BL-D2 removes — just under a different accounting line.
        Wherever it lands it is resident-unreachable (the daemon also runs with
        PrivateTmp=true) and the files themselves are 0600."""
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
        """Create the two 0600 output files for one build and return
        (out_path, err_path, out_fh, err_fh). mkstemp() creates them with mode
        0600 and O_EXCL, so no other local user can read a build's output and
        nothing can be pre-planted at the path. Separate files (not a single
        interleaved one) because _parse_build_report needs an uncorrupted
        stdout to find the session's final JSON report."""
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
        """The last `limit` bytes of a build output file, decoded leniently.
        BOUNDED BY CONSTRUCTION: seek to the end and read backwards, so the
        broker's memory cost is capped at `limit` no matter how much the build
        wrote. Never log or echo resident-influenced content unbounded."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - limit))
                data = fh.read(limit)
        except OSError:
            return ""
        return data.decode("utf-8", "replace")

    # ------------------------------------------- transient-unit lifecycle (L4)

    def _start_build_argv(self, key: str, default: list[str]) -> list[str]:
        """A fixed argv list out of `[start_build]`, validated like
        `_argv` validates `[commands]`. Same doctrine: config-supplied list,
        scalar args appended by the caller, shell never involved."""
        argv = self.start_build.get(key, default)
        if not isinstance(argv, list) or not argv or not all(
                isinstance(a, str) for a in argv):
            raise VerbError("internal",
                            f"start_build.{key} must be a non-empty list of strings")
        return list(argv)

    def _build_unit_state(self, slug: str) -> str:
        """systemd's word for what the build's unit is doing — `active`,
        `failed`, `inactive`, or `unknown` if we cannot ask. An UNPRIVILEGED
        read (`systemctl show`), unlike stopping it. A `--collect`ed unit that
        has finished no longer exists, and systemd answers `inactive` for
        anything it has never heard of: both are terminal, which is exactly the
        distinction the reaper needs."""
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

    def _stop_build_unit(self, slug: str) -> bool:
        """Ask systemd to stop a build's unit. THE ONLY WAY the cap still bites:
        the unit lives outside the broker's cgroup, so killing our local
        `sudo`/`systemd-run` process no longer kills the build. Routed through
        the same validating helper as the launch (`… stop <resident> <slug>`),
        so the sudoers rule stays two fixed shapes and nothing else.
        Best-effort by design: a build we cannot stop still dies at the helper's
        own RuntimeMaxSec backstop, and the failure is narrated either way."""
        try:
            argv = self._start_build_argv(
                "stop_command",
                ["sudo", "-n", "/usr/local/lib/disjorn/disjorn-build-launch", "stop"])
            resident_arg = str(self.start_build.get("resident", "gable"))
            cp = self._run([*argv, resident_arg, slug], 60)
            return cp.returncode == 0
        except Exception:  # noqa: BLE001 — never crash a reaper on cleanup
            return False

    def _sidecar_path(self, slug: str) -> str:
        return os.path.join(self._build_log_dir(), f"{slug}{BUILD_SIDECAR_SUFFIX}")

    def _write_build_sidecar(self, meta: dict, *, out_path: str, err_path: str,
                             timeout: int) -> str:
        """Persist everything a FUTURE broker process needs to finish this
        build's story: which unit, which branch, which spool files, and when the
        cap expires. Written BEFORE the launch (0600), so a broker that dies
        mid-spawn still leaves a trail rather than an orphaned unit nobody owns;
        removed on every terminal path alongside the spool files."""
        path = self._sidecar_path(meta["slug"])
        record = {
            "schema": BUILD_SIDECAR_SCHEMA,
            "slug": meta["slug"],
            "branch": meta["branch"],
            "unit": build_unit_name(meta["slug"]),
            # Two different residents, never conflate them: `caller` is who asked
            # (SO_PEERCRED, res-*), `build_resident` is the identity the unit
            # RUNS AS ([start_build].resident, no res- prefix).
            "caller": meta.get("resident"),
            "build_resident": str(self.start_build.get("resident", "gable")),
            "confirmed_by": meta.get("confirmed_by"),
            "seq": meta.get("seq"),
            "out_path": out_path,
            "err_path": err_path,
            # NO pid, deliberately. The only pid we have is the LOCAL
            # sudo/systemd-run process — precisely the thing that does not
            # survive a broker restart. The unit name is the durable handle.
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
        """Post a build state-transition line to #custodian via the broker's
        OWN bot identity — the same transport file-proposal uses. Best-effort:
        a posting failure never crashes a build (the audit line still lands).
        STATE TRANSITIONS ONLY — this is never called on a timer."""
        try:
            self.transport(self.disjorn, body)
        except Exception:  # noqa: BLE001 — narration is legibility, not control
            pass

    def _reap_build(self, proc: Any, spec_bytes: bytes, meta: dict,
                    timeout: int, out_path: str, err_path: str) -> None:
        """Detached-build lifecycle END (runs in a daemon thread; the request
        returned long ago). Feed the spec on stdin, wait up to the wall-clock
        cap, then narrate the terminal state transition — done or failed. No
        intermediate posts: a build that stalls goes quiet and fails loud at the
        cap (BUILD-LOOP: never timer-driven).

        BL-D2: `communicate()` is still how the spec is written and the cap is
        held — that part of the I/O contract is unchanged — but stdout/stderr
        are now FILES (see _default_build_spawn), so communicate() returns
        (None, None) and buffers nothing. The narration reads a BOUNDED TAIL
        (MAX_BUILD_LOG_TAIL) of each file instead. Both files are removed on
        EVERY exit path (done, failed, timed out, crashed) by the finally
        below, and the slug claim is released with them (BL-D4).

        WP-L4 open fork: `proc` is now the local `sudo systemd-run --pipe`
        process, not the build. It still carries the build's stdin, stdout,
        stderr and exit status (that is what --pipe means), so everything below
        reads the same — but killing it no longer kills the BUILD, which lives
        in a transient unit outside this broker's cgroup. So the timeout path
        stops the UNIT first and only then reaps the local process."""
        slug, branch = meta["slug"], meta["branch"]
        try:
            try:
                proc.communicate(spec_bytes, timeout=timeout)
            except subprocess.TimeoutExpired:
                stopped = self._stop_build_unit(slug)
                try:
                    proc.kill()
                    proc.communicate()
                except Exception:  # noqa: BLE001 — already reaping
                    pass
                self._narrate(format_build_failed(
                    slug=slug, branch=branch,
                    reason=f"timed out after {timeout}s — killed"
                           + ("" if stopped else " (unit stop reported a problem;"
                                                 " check systemctl status "
                                                 f"{build_unit_name(slug)})")))
                return
            except Exception as exc:  # noqa: BLE001 — broken pipe etc. = a failure
                self._narrate(format_build_failed(
                    slug=slug, branch=branch, reason=f"build error: {exc!r}"))
                return

            out_s = self._read_build_tail(out_path)
            err_s = self._read_build_tail(err_path)
            rc = getattr(proc, "returncode", None)
            if rc is not None and rc != 0:
                self._narrate(format_build_failed(
                    slug=slug, branch=branch,
                    reason=f"exit {rc}: {(err_s or out_s).strip()[:400]}"))
                return
            report = _parse_build_report(out_s)
            self._narrate(format_build_done(
                slug=slug, branch=branch, files=report["files"],
                tests=report["tests"], diff=report["diff"], tier="pending"))
        finally:
            self._unlink_build_logs(out_path, err_path)
            self._remove_build_sidecar(slug)
            self._finish_build(slug)

    # ------------------------------------------- reattachment after a restart

    def adopt_inflight_builds(self) -> list[str]:
        """Re-adopt builds that outlived the previous broker process, and sweep
        what did not survive. Called ONCE at startup, before serving.

        This is the other half of moving the build into a transient unit. The
        unit lives outside the broker's cgroup, so `systemctl restart
        disjorn-broker` no longer kills a build in flight — but the reaper
        thread still dies, and without this the build would finish into a spool
        file nobody reads, its done/failed line never posted and its slug never
        released. Each sidecar is one build's claim ticket:

          * unit still running  -> re-claim the slug (so a duplicate start-build
            is still refused) and start a polling reaper that narrates the
            terminal transition when it lands, exactly as the original would
            have. The original wall-clock deadline is carried in the sidecar, so
            a restart does not hand a build a fresh hour.
          * unit already gone   -> it finished while we were down: narrate from
            the spool tail (a parseable report means done; anything else is a
            loud, honest 'outcome unknown') and clean up.

        Returns the slugs adopted (running ones), for tests and the boot log.
        NEVER launches anything: adoption observes, narrates and tidies. It is
        also wrapped by main() so a surprise here can never stop the broker
        coming up — losing one narration must not cost every resident its
        hands."""
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
                # The ticket must be named after the build it claims, or the
                # slug inside decides which files get deleted while the
                # filename decides nothing — a mismatch is not a build record.
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
                # A build THIS process already owns: its own reaper will finish
                # the story. Keeping it out of the sweep makes adoption safe to
                # call at any moment, not only before the socket is open.
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
        # Janitor: spool files with no live ticket are orphans from a broker
        # that died mid-build. Nothing will ever read them and nothing else ever
        # deletes them, so they are the one way build-logs grows without bound
        # across restarts. Sweep them here, where we know which files are live.
        for name in entries:
            if name in keep or not name.startswith(BUILD_UNIT_PREFIX):
                continue
            if name.endswith(".out") or name.endswith(".err"):
                self._unlink_build_logs(os.path.join(log_dir, name))
        return adopted

    def _reap_adopted_build(self, rec: dict) -> None:
        """Watch an adopted build to its terminal state, then narrate + tidy.
        Polls systemd rather than waiting on a pipe — we are not this process's
        child any more. The deadline is the ORIGINAL one from the sidecar; past
        it we stop the unit, exactly as the first reaper would have.

        The ticket is torn up ONLY on a terminal state. If this broker is itself
        shutting down (or the poll blows up) the build is still out there, so the
        sidecar and the spool files stay exactly where the NEXT process will look
        for them — losing the ticket while the build runs is the one way to
        strand it for good. Narration is therefore at-least-once, never
        at-most-once: a duplicated done line is noise, a missing one is a build
        nobody hears about."""
        slug = rec["slug"]
        try:
            deadline = float(rec.get("deadline") or 0.0)
            while not self._closed:
                state = self._build_unit_state(slug)
                if state not in BUILD_ACTIVE_STATES:
                    self._narrate_adopted_outcome(rec, state)
                    break
                if deadline and time.time() > deadline:
                    self._stop_build_unit(slug)
                    self._narrate(format_build_failed(
                        slug=slug, branch=rec.get("branch", f"loop/{slug}"),
                        reason=f"timed out after {rec.get('timeout_sec')}s "
                               "— killed (build re-adopted after a broker restart)"))
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
        """The done/failed line for a build this process did not launch.

        There is no exit status to read: `--collect` unloads the unit when it
        ends, and systemd cannot tell us about a unit it has forgotten. So the
        REPORT is the evidence — the build session's final JSON on stdout. If it
        is there the build finished its work and we say done; if it is not we
        say so plainly rather than guessing, because a build that vanished
        mid-flight and a build that failed are the same thing to a reviewer:
        look at the branch."""
        slug = rec["slug"]
        branch = rec.get("branch", f"loop/{slug}")
        out_s = self._read_build_tail(str(rec.get("out_path") or ""))
        err_s = self._read_build_tail(str(rec.get("err_path") or ""))
        report = _parse_build_report(out_s)
        if state == "failed" or report["files"] == "n/a":
            reason = (err_s or out_s).strip()[:400]
            self._narrate(format_build_failed(
                slug=slug, branch=branch,
                reason=("re-adopted after a broker restart and left no report "
                        f"on stdout (unit state {state}) — outcome unknown, "
                        "check the branch"
                        + (f": {reason}" if reason else ""))))
            return
        self._narrate(format_build_done(
            slug=slug, branch=branch, files=report["files"],
            tests=report["tests"], diff=report["diff"], tier="pending"))

    def _verb_start_build(self, resident: str, args: dict) -> tuple[dict, str]:
        """Launch a DETACHED build of a CONFIRMED spec to `loop/<slug>` (WP-L4).

        The gate, in order and all mechanical (chat is data, never
        authorization — the verbs.toml toggle authorizes the CLASS, this
        resident may run builds; the confirm record in the file selects the
        INSTANCE and the broker verifies it, never trusts it):
          0. SPECS/ itself is resident-unwritable — asserted at broker STARTUP
             (BL-D1, assert_specs_dir_resident_unwritable); without it every
             check below is self-attestation;
          1. the spec path resolves inside SPECS/ (no `..`, no symlink escape);
          2. the spec's status is 'confirmed' with a real confirm record
             (Confirmed by + #custodian seq) — else refuse, fail-loud;
          3. no build of this slug is already in flight (BL-D4) and the per-day
             build budget has a free slot — both claimed under one lock.
        On accept it posts a 'started' line to #custodian, spawns the build
        detached (own session; outlives this request), and returns immediately.
        A daemon reaper feeds the spec on stdin, holds the wall-clock cap, and
        narrates done/failed. The build lands on the branch; NOTHING merges,
        pushes, or touches production."""
        _reject_unknown(args, {"spec"})
        spec_arg = _check_str(args, "spec", required=True, max_len=300)
        assert spec_arg is not None
        spec_path = self._resolve_spec_path(spec_arg)
        meta = self._read_confirmed_spec(spec_path)

        # Build the argv (pure config + validated slug) BEFORE reserving, so a
        # misconfiguration refuses without burning a budget slot.
        argv = self._build_argv(meta["slug"])
        timeout = int(self.start_build.get("timeout_sec", START_BUILD_DEFAULT_TIMEOUT))
        prompt = build_session_prompt(
            meta["text"], slug=meta["slug"], branch=meta["branch"])

        # Reserve the budget slot + claim the slug under the lock (H13-D4,
        # BL-D4). A refusal here is audited (over-budget / bad-args) like any
        # other denial and burns nothing.
        used, cap = self._reserve_build(resident, meta["slug"])

        # BL-D2: the build's stdout/stderr land in 0600 temp FILES, never in
        # pipes this privileged process must drain. Opened after the budget
        # claim so a refused build creates no files; removed on every exit path
        # below and in the reaper's finally.
        try:
            out_path, err_path, out_fh, err_fh = self._open_build_logs(meta["slug"])
        except BaseException:
            self._release_build(resident, meta["slug"])
            raise

        # The claim ticket for the transient unit, written BEFORE the launch so
        # a broker that dies mid-spawn still leaves a trail for the next process
        # to adopt (adopt_inflight_builds). Removed on every terminal path.
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

        # 'started' — a state transition; best-effort (a failed post must never
        # sink a launched build, and is never a heartbeat).
        self._narrate(format_build_started(
            slug=meta["slug"], branch=meta["branch"],
            confirmed_by=meta["confirmed_by"], seq=meta["seq"], eta_sec=timeout))

        try:
            proc = self._build_spawn(argv, stdout=out_fh, stderr=err_fh)
        except OSError as exc:
            # Never spawned: refund the slot, drop the slug claim, delete the
            # (empty) output files. BL-D3: this path audits allowed=True
            # (exec-failure, not a denial) but emits NO `build_started` marker,
            # so a restart's reseed does not count it.
            self._release_build(resident, meta["slug"])
            self._close_build_logs(out_fh, err_fh)
            self._unlink_build_logs(out_path, err_path)
            self._remove_build_sidecar(meta["slug"])
            self._narrate(format_build_failed(
                slug=meta["slug"], branch=meta["branch"],
                reason=f"launch failed: {exc}"))
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
                  # The transient unit the build runs in. Derivable from the
                  # slug, surfaced anyway: it is the one string that makes a
                  # running build inspectable (`systemctl status <unit>`), and
                  # `pid` alone is now the LOCAL sudo/systemd-run process, not
                  # the build.
                  "unit": build_unit_name(meta["slug"]),
                  "confirmed_by": meta["confirmed_by"], "seq": meta["seq"]}
        budget_str = f"{used}/{cap}" if cap is not None else str(used)
        # Third element = audit extras. `build_started` is the BL-D3 marker:
        # the ONLY place it is emitted is here, after a successful spawn, so
        # the audit log distinguishes "this build ran" from "this call was
        # authorized but never launched a thing".
        return (result,
                f"build {meta['slug']} -> {meta['branch']} launched "
                f"(budget {budget_str})",
                {"build_started": True})

    def _verb_classify_diff(self, resident: str, args: dict) -> tuple[dict, str]:
        """Contract with harness/classifier/classify_diff.py (WP-H4):
        argv: <classify_diff.py> --repo <abs path> --range <git range>
              --config <protected-paths.toml> --gates <json object>;
        stdout: one JSON object (the classification), exit 0. Anything else
        is exec-failure. --config comes from broker config, never from the
        caller — the classifier config is protected by placement."""
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
        # WP-H13 F3: the classifier splits A..B (or A...B) and hands each side
        # to git as a bare positional. A leading '-' on the WHOLE string is
        # rejected above, but the RIGHT side after the split can still start
        # with '-' (e.g. "main..--exit-code") and reach git as a flag. Reject
        # a leading dash on EITHER side of the split — no ref legitimately
        # starts with one.
        for _side in rng.replace("...", "..").split(".."):
            if _side.startswith("-"):
                raise _bad("neither side of the range may start with '-'")
        # Residents pass THEIR view of the filesystem; the broker runs
        # host-side where those paths don't exist. [residents.<r>.path_map]
        # translates container prefixes to host paths (longest prefix wins)
        # AND is the allowlist: a repo outside every mapped root is rejected,
        # so a resident can only ever point the classifier at repos
        # deliberately exposed to them.
        #
        # WP-H13 F2: absent map now FAILS CLOSED. It used to pass the caller's
        # repo through verbatim, so a resident configured without a map could
        # aim git at any host path the broker uid can read. A resident allowed
        # to classify must have an explicit map; no map = no classify.
        path_map = self.residents.get(resident, {}).get("path_map")
        if not path_map:
            raise _bad(f"no classify-diff path_map configured for {resident}; "
                       "classify-diff requires an explicit repo allowlist")
        best = max((p for p in path_map
                    if repo == p or repo.startswith(p.rstrip("/") + "/")),
                   key=len, default=None)
        if best is None:
            raise _bad(f"repo not under a mapped root for {resident}; "
                       f"available roots: {sorted(path_map)}")
        repo = path_map[best].rstrip("/") + repo[len(best.rstrip("/")):]
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
        """Tail/grep of the CALLING resident's configured log file only. The
        path comes from broker.toml; a caller-supplied `path` is accepted only
        if it resolves to exactly that file (so `../` games are dead ends)."""
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
        if grep is not None:  # plain substring match in-process — no shell, no regex
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
        """The calling resident's OWN audit lines for a date range. Filtering is
        by the broker-assigned resident name — never a caller-supplied value —
        so nobody can read anyone else's trail."""
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

    # ------------------------------------------------------------- server

    def serve_forever(self) -> None:
        sock_dir = os.path.dirname(self.socket_path)
        if sock_dir and not os.path.isdir(sock_dir):
            os.makedirs(sock_dir, exist_ok=True)
        # Remove a stale socket left by an unclean shutdown (only if it IS a socket).
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
        # A blocked accept() is not interrupted by close() on Linux, so poll
        # with a short timeout; shutdown() additionally pokes the socket.
        listener.settimeout(1.0)
        self._listener = listener
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
                    # WP-H13 F1: audit this rejection like every other. The
                    # invariant (PROTOCOL.md, brokerd docstring) is that every
                    # request leaves exactly one line; the oversize path used
                    # to return silently, letting a resident spam hostile
                    # requests with no trace.
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
        # BL-D1 and friends: an unsafe config is a REFUSAL TO START, printed
        # loudly and exited non-zero (systemd Restart=on-failure will retry and
        # the failure stays visible in `systemctl status`). Never degrade to
        # "start anyway without that verb" — a gateway that quietly drops a
        # guarantee is the thing this whole file exists to prevent.
        print(f"disjorn-broker: REFUSING TO START — {exc}", file=sys.stderr)
        return 2

    def _stop(signum: int, _frame: Any) -> None:
        print(f"disjorn-broker: signal {signum}, shutting down", file=sys.stderr)
        broker.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # WP-L4: builds run in transient units OUTSIDE this daemon's cgroup, so a
    # restart no longer kills one in flight — but its reaper died with the old
    # process. Re-adopt before serving so the narration still lands. Never fatal:
    # a gateway that refuses to come up because it could not tidy a log file
    # would take every resident's hands away over a cosmetic failure.
    try:
        adopted = broker.adopt_inflight_builds()
        if adopted:
            print(f"disjorn-broker: re-adopted in-flight builds: "
                  f"{', '.join(adopted)}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"disjorn-broker: WARNING build re-adoption failed: {exc!r}",
              file=sys.stderr)

    print(f"disjorn-broker: listening on {broker.socket_path} "
          f"(config={ns.config}, verbs={ns.verbs})", file=sys.stderr)
    broker.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
