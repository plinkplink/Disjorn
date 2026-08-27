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
* One caller is not a resident: `wake` (2026-08-25 agentic residents) is
  called by plink's own uid from the keyboard and by nothing else. A seat may
  never call it and a wake caller may call nothing else — both in code
  (_check_wake_identity), on top of the kill switch. That is what makes a
  wake's origin connection data rather than something a message could say.
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
# Plan Room (SPECS/2026-08-20-plan-room.md). Skim is the default and detail is
# opt-in: `board-list` returns ONE LINE per card and `board-card` returns the
# whole thing, because the request that started this feature named the context
# window as the problem — "so that your entire context window isn't swallowed
# by reading the whole thing all the time".
MAX_BOARD_CARDS = 200
MAX_BOARD_COMMENT_CHARS = 4000
MAX_BOARD_REASON_CHARS = 500
MAX_BOARD_SEARCH_CHARS = 200
# A card slug, anchored. The two prefixed forms are the derivation's cards for
# things with no spec file yet (a backlog row, a keyboard commit).
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

# ---------------------------------------------------------------- publish lines
# SPECS/2026-08-13-build-publish-path.md item 3. The build session no longer
# pushes anything: after the container exits, run-build.sh harvests HOST-side
# (as res-<name>, where the gatehouse group actually exists) and prints one
# machine-readable line per entitled repo. Those lines are the ONLY evidence the
# reaper has, and the only evidence it is allowed to have — the harvest IS the
# verification, and the reaper measures nothing itself (one mechanism; two can
# disagree).
#
# Shapes, all anchored at line start because they arrive INTERLEAVED with
# session output in the same spool file and a lookalike mid-sentence must never
# be read as a measurement:
#   PUBLISHED <repo>.git <sha>            branch verified by rev-parse IN the
#                                         gatehouse after a plain (no-force) push
#   PUBLISH-FAILED <repo>.git <git error> push or verification failed, verbatim
#   NO-COMMITS <repo>.git                 honest zero-work line
#   QUARANTINED <repo> <path>             provisioning moved an unharvested
#                                         previous clone aside (printed BEFORE
#                                         the session runs)
# ABSENCE of all of them on a unit that exited 0 is itself the failure signal —
# a timeout-killed wrapper skips the harvest by design and prints nothing.
_PUBLISH_REPO_RE = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_PUBLISH_LINE_RES = (
    ("published", re.compile(
        rf"^PUBLISHED[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]+([0-9a-fA-F]{{7,64}})"
        r"[ \t]*$")),
    ("failed", re.compile(
        rf"^PUBLISH-FAILED[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]+(\S.*)$")),
    ("no_commits", re.compile(
        rf"^NO-COMMITS[ \t]+({_PUBLISH_REPO_RE}\.git)[ \t]*$")),
    # The quarantine line names the repo WITHOUT .git (it is a workspace clone,
    # not a bare repo) and carries a path we only ever echo, never open.
    ("quarantined", re.compile(
        rf"^QUARANTINED[ \t]+({_PUBLISH_REPO_RE})[ \t]+(\S.*)$")),
)
# Bounds on what reaches the banner. The banner is posted to #custodian through
# the same un-truncated path file-proposal uses, and every field below is
# wrapper/git text: cap the COUNT (a stuck loop cannot flood the channel) and
# the LENGTH of each free-form field. Two entitled repos is the real number; 8
# leaves headroom without letting a banner become a log dump.
MAX_PUBLISH_LINES = 8
MAX_PUBLISH_ERR_CHARS = 200
MAX_QUARANTINE_PATH_CHARS = 160

# ---------------------------------------------------------------------- wake
# SPECS/2026-08-25-agentic-residents.md. A wake starts a headless work session
# in a resident's seat. It is the ONE verb whose caller is not a resident, and
# the reason it is a verb at all is authentication: the broker resolves the
# caller from SO_PEERCRED, so a wake's origin is connection data and no text in
# any channel can constitute one.
WAKE_VERB = "wake"
MAX_WAKE_TASK_CHARS = 4000
# One record per wake, in the plink-owned spool. The seat's wake runner reads
# them; it cannot write the directory, which is what makes "nothing self-wakes"
# a placement property rather than a promise (same wall as start_build's
# specs_dir — see assert_dir_resident_unwritable).
WAKE_SPOOL_SUFFIX = ".wake.json"
WAKE_SPOOL_SCHEMA = 1
# Wall-clock cap for a woken session, in seconds. Longer than a summon (600s in
# the shipped summon.toml) because a wake is a work session, not a chat turn.
# It lives HERE, in plink-owned broker config, and rides on the wake record —
# so the seat's runner enforces a cap it cannot widen, and there is one value,
# not two that can drift.
DEFAULT_WAKE_SESSION_CAP_SEC = 5400
# How long after the cap a wake is still considered in flight. Covers the
# runner's own harvest + post, and is the margin after which a wake with a
# start and no end in the action log is an incident.
DEFAULT_WAKE_GRACE_SEC = 600
# How long a record stays in the spool after its window closes. NOT the window:
# a record whose window has passed is exactly what lets a runner that was DOWN
# come back and post "this wake was missed" instead of leaving a human waiting
# on silence. Pruning on the window would delete that evidence out from under
# the runner, so the spool holds a week and the runner decides what is stale.
WAKE_RETENTION_SEC = 7 * 86400
# Wakes per seat per UTC day, CAPPED BY DEFAULT — an unset cap is not "no
# policy", it is an unbounded number of 5400s account-billed sessions behind one
# button. Same shape as start_build's daily_build_cap, and widened only by a
# witnessed edit to plink-owned config.
DEFAULT_DAILY_WAKE_CAP = 3
_WAKE_ID_RE = re.compile(r"^wake-\d{8}T\d{6}Z-[0-9a-f]{6}$")


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
    must be the same string."""
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
    naming the offending path. Returns realpath(directory), which the caller
    should use from then on — the checked path and the used path must be the
    same string.

    Two authorization surfaces rest on this: start_build's SPECS/ (BL-D1, the
    confirm record) and the wake spool (2026-08-25, "nothing self-wakes"). Both
    are presence checks on text that only mean anything because the resident
    cannot write the text; `label`, `remedy` and `stake` are what each caller
    tells the reader about its own.

    Pure with respect to broker state: it takes the uid map, the residents
    table and (injectably) the uid->gids resolver, so every adversarial case
    is testable without creating real users."""
    if broker_uid is None:
        broker_uid = os.geteuid()
    real = os.path.realpath(directory)

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
# Plan Room API transport (SPECS/2026-08-20-plan-room.md). Kept behind a
# callable so tests stub it, exactly like _sdk_transport above.
# --------------------------------------------------------------------------

def _planroom_http(disjorn_cfg: dict, method: str, path: str,
                   payload: Optional[dict] = None) -> dict:
    """One JSON call to the Disjorn server's /planroom surface, as the broker's
    own bot identity.

    WHY THE BOARD VERBS GO THROUGH THE SERVER RATHER THAN READING TWO FILES.
    A card is derived state plus board-native state — the derived half is in
    the broker-written index, the native half (comments, order, blocked,
    archived) is authoritative and lives in the server's own tables. Composing
    them is exactly one job, and the server already does it for the tab. A
    second composer here would be a second answer to "is this card blocked",
    which is the forked-truth failure this whole spec is built to avoid. So the
    broker asks the server the same question the tab asks.

    The WRITES have a second reason: those tables are the server's, and the
    resident-facing wall on them is the server's `admin or bot` check. Writing
    them from here would be the broker granting itself an exemption from the
    rule it exists to enforce."""
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
        # The server's refusal is carried through verbatim. A resident who is
        # told "the Plan Room index is unavailable" can act; one told "HTTP
        # 503" has to go find someone.
        raise VerbError("exec-failure",
                        detail or f"plan room API returned {exc.code}") from None
    except Exception as exc:  # noqa: BLE001 — network, DNS, timeout, bad JSON
        raise VerbError("exec-failure",
                        f"plan room API unreachable: {exc}") from None


def _urlq(value: str) -> str:
    import urllib.parse
    return urllib.parse.quote(value, safe="")


_PLANROOM_MODULE = None


def _load_planroom_module():
    """`harness/planroom/planroom.py` — the derivation service.

    Imported LAZILY, and only when a rebuild actually runs. Two reasons, both
    load-bearing:

      * It imports this module back (for `parse_spec_status` and
        `parse_confirm_record` — the gate's own parsers, always, per seq 1428
        P3). A top-level import here would be a cycle. Lazily, it finds this
        module already in `sys.modules` and reuses it, which is also how there
        stays exactly one copy of the parsers in the process.
      * It reaches for host paths (the gatehouse, the message store) at import
        time, and none of that belongs in the daemon's import graph or in test
        collection."""
    global _PLANROOM_MODULE
    if _PLANROOM_MODULE is not None:
        return _PLANROOM_MODULE
    import importlib.util
    # Hand the derivation service THIS module as `brokerd`. Run as a daemon
    # this file is `__main__`, so without this line planroom's parser lookup
    # would miss it and load a second copy of the broker — two parsers of one
    # Status line again, by the one route the P3 rule did not name.
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
    """One card, one line. brief's rule, inherited: NEVER PRINT A BARE
    IDENTIFIER — every row says what the thing is and where it lives, because
    an item you have to go look up is an item that gets deferred."""
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
    """The board's own staleness, said out loud.

    The board cannot go stale relative to the mirror — it is not a copy of it —
    but the MIRROR can lag, so every renderer says which mirror head it derived
    from and when. Staleness in this house is declared, never denied."""
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



def replace_spec_status(text: str, new_status: str, comment: str) -> Optional[str]:
    """Rewrite the `## Status` token in a spec to `new_status`, followed by ONE
    HTML comment line saying who moved it and why. Returns the new text, or
    None if the file has no parseable Status line (the caller then leaves the
    file alone — a spec the gate cannot read is not one this should invent a
    section in).

    Only the FIRST non-blank, non-comment line under the heading is replaced —
    the same line parse_spec_status reads — and everything else in the file
    (the confirm record above all) is byte-for-byte untouched. The board's
    `--mark-merged` and the broker's build stamps both go through here, so a
    Status line always has one shape and one parser."""
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
    """Make resident-influenced text safe INSIDE an HTML comment. A build's
    failure reason comes from the build's own output, so it could carry `-->`
    (closing the comment early and putting a line of its choosing where the
    parser reads the status) or a newline. Collapse whitespace, break every
    `--` run, cap the length. Never write build output into SPECS/ unfiltered."""
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
    """(status token, comment) the spec should carry once its build is
    terminal. TEMPLATE.md's vocabulary, no new words:
      * published                 -> `built@<branch>`  (work is on the branch,
                                     waiting for review; NOT buildable again)
      * failed (any way)          -> `failed`          (a human is told to look;
                                     set it back to `confirmed` to allow
                                     another attempt — the confirm record still
                                     stands, nothing here touches it)
      * only NO-COMMITS lines     -> `confirmed`       (the build ran and
                                     produced nothing: no branch, nothing to
                                     review, so it is honestly buildable again)
    The comment records what happened, sanitized (_status_comment_text)."""
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
        # MATCH THE WORDS, NOT THE ASTERISKS. Twice now a spec every seat had
        # signed was invisible to this gate because of markdown placement:
        # `**Confirmed** by:` (bold closing one word early, 2026-08-17) parses
        # as no record at all, and so would `Confirmed by:` with no bold, or
        # `**Confirmed by:**` with the colon inside. The gate's job is to
        # verify WHO and WHICH SEQ — never to grade a human's markdown. So
        # strip emphasis from the line first, then match the plain phrase.
        # (Still `- ` bullets, still first match wins, still `<…>` = unset.)
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


# BR-1 (2026-08-14). The identity a build RUNS AS is derived from the identity
# that ASKED — the SO_PEERCRED-resolved caller — never from configuration.
#
# Until today `[start_build].resident` was a single global name, so every build
# ran as res-gable whoever pressed: Claudette's 08-14 password build was called
# by res-claudette (audit), ran in res-gable's home on res-gable's credential
# (process), and its commit says disjorn-build (git). Three records, no two
# agreeing, and no way to tell from any of them whose judgement produced the
# diff. Worse, the misattribution CONCEALED a second defect for a week: her
# build seat had no account credential, and the wrapper's refusal never fired
# because her builds were never actually hers.
#
# The caller arrives as the uid_map name ("res-claudette"); the launch helper
# takes the short name ("claudette") and re-derives everything — uid, home,
# config dir — from it. The regex is deliberately the helper's own RESIDENT_RE
# so the two programs can never disagree about what a resident is called.
_BUILD_CALLER_RE = re.compile(r"^res-([a-z][a-z0-9]{0,30})$")


def build_identity_from_caller(caller: str) -> str:
    """Short build identity ("claudette") from a uid_map caller name
    ("res-claudette"). Raises VerbError on anything else — an unparseable
    caller must refuse loudly, never fall back to some configured default,
    because a fallback identity is exactly the bug this function removes."""
    m = _BUILD_CALLER_RE.match(caller or "")
    if not m:
        raise VerbError("internal",
                        f"cannot derive a build identity from caller {caller!r} "
                        "(expected res-<name>); refusing rather than guessing")
    return m.group(1)


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
    """The committed spec plus a one-paragraph preamble, fed to the build
    session on STDIN. ALL of it is data on stdin — argv stays config-only
    (launcher doctrine): only the mechanically-validated slug/branch and fixed
    broker text vary here.

    2026-08-06 (branch B): this used to restate the rules — no merge, no push,
    no prod, the report format — which meant they lived in TWO places and could
    drift apart. They now live only in the build seat's CLAUDE.md
    (harness/cc/build-kernel.md), which the wrapper copies into the build home
    before launch. This function states the TASK; the kernel states the
    CONTRACT. Do not re-add rules here: a rule in two places is a rule that
    will eventually say two things.

    The old text also told the session to narrate state transitions to
    #custodian. It no longer can and no longer should — the build seat has no
    broker socket (see run-build.sh) and its report IS its stdout, which the
    reaper reads and posts."""
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
    """Pull a JSON object out of a chunk of model prose.

    THE BUG THIS FIXES (2026-08-06). The first successful resident build posted
    `files: n/a | tests: n/a | diff: n/a` to #custodian while its branch carried
    246 changed lines and 111 passing tests. The build was fine; the REPORT of
    it was empty, which is this house's worst failure shape — a record that says
    nothing happened when something did.

    Why: the session runs under `claude -p --output-format json`, so stdout is
    ONE envelope object whose `result` is the assistant's final text. That text
    is prose with the report in a ```json fence, exactly as any model writes it.
    The old code called `json.loads` on that string, got a JSONDecodeError, and
    fell through to the n/a defaults — so the nicer the session's write-up, the
    more certainly its report was discarded.

    Three attempts, cheapest first: the whole string, a fenced block, then the
    last balanced {...} span. Returns None rather than guessing.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Fenced blocks: take the LAST one — the report is the closing artifact,
    # and a spec quoted earlier in the reply may itself contain a fence.
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
    """Best-effort structured report from the build session's stdout for the
    'done' line. The session is asked to end with a JSON object
    {files, tests, diff, branch}; we surface those and degrade to 'n/a' (or a
    text tail) if it didn't. Tier is intentionally NOT computed here — see
    format_build_done: classify-diff is a separate verb, not coupled in.

    BL-D2: the input is now the bounded TAIL of the build's stdout file, not
    the whole stream, so it may begin mid-line. Hence the second attempt on the
    last non-blank line — the report is the last thing printed, and a truncated
    head must not cost us the report.

    2026-08-13: the report is no longer the last thing on stdout — the wrapper's
    harvest prints after the container exits. Callers pass the tail through
    _strip_publish_lines first, so "the last non-blank line" still means the
    SESSION's last line. This report is enrichment now; the publish lines decide
    whether a build is done."""
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
    publish-protocol line. Anchored at the line START and strict about shape, so
    a session that WRITES about `PUBLISHED foo.git deadbeef` in its prose (or a
    log line that embeds one) can never be read as a measurement."""
    for kind, rx in _PUBLISH_LINE_RES:
        m = rx.match(line)
        if m:
            return kind, m.groups()
    return None


def _parse_publish_lines(out: str) -> dict:
    """The wrapper's harvest report, extracted from a build's stdout.

    Returns {published: [(repo, sha)], failed: [(repo, error)],
             no_commits: [repo], quarantined: [(repo, path)]} — measurements
    only, in the order printed. THE REAPER MEASURES NOTHING ITSELF: everything
    the banner says about what left the container is one of these lines, because
    the harvest is the verification and a second verification path could only
    ever disagree with it (SPECS/2026-08-13-build-publish-path.md item 3).

    Deliberately total and quiet: unparseable input yields empty lists, which
    the caller must read as FAILED (absent lines = failure), never as success.
    Duplicates collapse — the reaper feeds this the log's HEAD and TAIL, which
    can overlap on a small file, and one repo published twice is a wrapper bug
    not two publications. Every list is capped (MAX_PUBLISH_LINES) and every
    free-form field truncated: this text goes to #custodian unbounded otherwise.
    """
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
    """The same stdout with the wrapper's protocol lines removed — what the
    SESSION printed, which is what _parse_build_report must see. The harvest
    prints after the container exits, so its lines land AFTER the session's
    final JSON report; feeding them to the report parser (which reads the last
    non-blank line) would throw the report away on every successful build."""
    return "\n".join(ln for ln in (out or "").splitlines()
                     if _match_publish_line(ln.rstrip("\r")) is None)


def _publish_reported(publish: dict) -> bool:
    """Did the harvest report a VERDICT for any repo? Quarantine lines
    deliberately do not count: provisioning prints them before the session even
    starts, so a quarantine line plus silence still means the harvest never
    ran."""
    return any(publish.get(k) for k in ("published", "failed", "no_commits"))


def _quarantine_suffix(quarantined) -> str:
    """Quarantine notices, one line each, appended to WHATEVER banner results.
    A quarantined clone is work that was preserved instead of deleted (the
    08-13 rescue that only happened because a human posted a warning); it is
    never allowed to be the silent part of a message."""
    return "".join(
        f"\nquarantined: {repo} -> {path} — unharvested work from an earlier "
        f"run, preserved not deleted" for repo, path in quarantined)


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
                      diff: str, tier: str = "pending", published=(),
                      no_commits=(), quarantined=(), mirror: str = "") -> str:
    """The 'done' state-transition line. Its LOAD-BEARING field is now what the
    wrapper measured — `published: <repo>.git <sha>` per entitled repo, or the
    honest 'no commits' line when the build produced none. files/tests/diff are
    the session's own report and stay as ENRICHMENT: publish lines decide truth,
    the report decorates. Tier is 'pending' by default — the reaper does not
    invoke classify-diff (a separate verb, ships OFF) and, per the 08-13 spec,
    runs no verification of its own at all. Nothing merged, ever.

    'on the branch for review' without a measured sha is deliberately
    unprintable from here: with no PUBLISHED line the caller never reaches this
    formatter (see format_build_outcome)."""
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
# Shared constant rather than a literal 78 in two files — the wrapper and this
# reader must always mean the same thing by it, and a silent disagreement would
# turn a refund into a burned slot.
PREFLIGHT_REFUSED_EXIT = 78


def format_build_refused(*, slug: str, branch: str, reason: str) -> str:
    """A build that never started, because its seat could not have run the
    tests the spec asks for.

    Deliberately NOT worded as a failure. Nothing was built and nothing was
    lost; the honest reading is that the house caught its own unfitness before
    spending anything, which is the outcome the preflight exists to produce.
    The banner says the slot was refunded so nobody has to go and check."""
    detail = " ".join(reason.split())[:400]
    return (f"build refused | {slug} -> {branch} | nothing ran, no slot spent | "
            f"{detail or 'the build seat failed its dependency preflight'}")


def format_build_failed(*, slug: str, branch: str, reason: str, published=(),
                        no_commits=(), quarantined=(), mirror: str = "") -> str:
    """The 'failed' state-transition line — LOUD. A stalled build goes quiet
    then lands here (never a heartbeat) and a human is told to look.

    It also states WHERE THE WORK IS, from the harvest lines and nothing else: a
    failed build that published something must say so, and one that published
    nothing must not imply a branch that does not exist (the phantom-branch
    claim the 08-13 spec exists to retire)."""
    if published:
        where = ("published anyway: "
                 + ", ".join(f"{repo} {sha}" for repo, sha in published))
    elif no_commits:
        where = "nothing published — no commits, no branch exists"
    else:
        where = "nothing published — no branch to review"
    return (f"BUILD FAILED | {slug} -> {branch} | {reason} | {where} | "
            f"a human should look" + _quarantine_suffix(quarantined) + mirror)


# The fail-closed clause. A wrapper that is killed at the cap skips its harvest
# BY DESIGN and prints nothing, and a wrapper that predates the publish contract
# also prints nothing: silence is indistinguishable between them and must never
# be read as success. This is the one banner the reaper prints from an ABSENCE.
NO_HARVEST_REASON = (
    "the wrapper printed no publish lines — the harvest never reported "
    "(killed at the cap, or a wrapper predating the publish contract): "
    "outcome unknown, nothing was published")


def format_mirror_note(branch: str, published, error: "str | None") -> str:
    """The one line that makes a PUBLISHED banner openable (spec item 5).

    A banner names a sha. Until the mirror has been re-fetched, that sha exists
    only in the gatehouse — which no resident can read — so the line names
    something its audience cannot open, and the reviewer's first move is to ask
    for a refresh. Refreshing FIRST and then saying where to look costs one
    fetch and removes the round trip.

    On success it prints the exact ref to rev-parse, because "it's in the
    mirror somewhere" is not an instruction. On failure it says so plainly
    rather than staying silent: the sha in the banner above is then real but
    unreadable, and a reader must be told which of the two they are holding."""
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
    Status line — moved (to what, in which commit) or NOT moved (and why). The
    file is the state of record (SPECS/README.md: 'state lives in the file'),
    so a stamp that failed has to be said out loud where the humans are, or the
    next resident reads a stale word and rebuilds."""
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
    """THE decision: done or failed, from the wrapper's harvest lines. One
    implementation, called by both reapers (the live one and the adopted one) —
    two copies of this ladder would eventually narrate two different truths
    about the same build.

    In order, and the order is the contract:
      1. the unit itself failed (`unit_reason`) -> FAILED, carrying whatever the
         harvest still managed to report (a container can die clean and the
         push still be rejected);
      2. ANY PUBLISH-FAILED line -> FAILED with the verbatim git error(s);
      3. at least one PUBLISHED line -> done, naming repo + sha;
      4. only NO-COMMITS lines -> done, honestly: no commits, no branch;
      5. nothing at all -> FAILED (NO_HARVEST_REASON). Never assume success
         from silence."""
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
#
# plink wakes a seat with a task; the seat's runner works it in one headless
# session and posts the result. Everything the broker contributes is here: an
# id, the record the runner reads, and the two parsers behind the no-self-review
# rule a woken session inherits.
# --------------------------------------------------------------------------


def new_wake_id(now: Optional[_dt.datetime] = None,
                entropy: Optional[str] = None) -> str:
    """`wake-20260825T142310Z-9f3a1c` — sortable, greppable, collision-safe.

    The timestamp is what a human reads in #custodian and in the action log;
    the suffix is what keeps two wakes in the same second apart. Both halves
    are needed: the id is the only string that ties a broker audit line, an
    action-log start/end pair and a #custodian post to one another."""
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
    """The wall a wake past the daily cap hits.

    The wall clock rides next to the count because the minutes are the cost and
    the count is only the speed bump: three wakes at a 5400s cap is most of an
    afternoon of billed session, and a reader who sees `3/3` alone learns the
    smaller of the two numbers."""
    return (f"daily wake cap reached for {seat}: {count}/{cap} wakes, "
            f"{format_session_time(spent_sec)} of session time today. Next "
            f"wake is tomorrow (UTC), or a witnessed edit to "
            f"[wake].daily_wake_cap in broker.toml.")


def parse_review_owner(text: str) -> Optional[str]:
    """The `- **Review owner**: …` bullet's value, or None if the spec has no
    such bullet.

    Emphasis is stripped before matching, for the reason parse_confirm_record
    strips it: the gate's job is to read a field, never to grade markdown.
    None means the spec does not state a review owner — which is NOT the same
    as stating one that is nobody, and the woken-build check treats the two
    differently."""
    for line in text.splitlines():
        plain = re.sub(r"[*_`]", "", line)
        m = re.match(r"\s*-\s*Review owner\s*:\s*(.*)$", plain, re.I)
        if m:
            return _clean_field(m.group(1))
    return None


def review_owner_seat(raw: Optional[str],
                      known_seats: "set[str] | frozenset[str]") -> Optional[str]:
    """The SEAT a review-owner line names (`Claudette` -> `res-claudette`), or
    None when it names nobody this house runs as.

    Only the first name-shaped token is considered: the line is prose after the
    name in every spec that has one ("Claudette. The builder cannot
    self-review, and…"). A line naming a human (`plink`) resolves to no seat,
    and correctly so — a human review owner is the case the no-self-review rule
    exists to protect, not a case it should refuse."""
    if not raw:
        return None
    m = re.match(r"[\s*_`]*([A-Za-z][A-Za-z0-9_-]{0,30})", raw)
    if not m:
        return None
    seat = f"res-{m.group(1).lower()}"
    return seat if seat in known_seats else None


# --------------------------------------------------------------------------
# Bot-to-bot summon hops (SPECS/2026-08-24-custodian-mention-summons.md).
#
# The wall a work loop runs against. Guard 1 is the default and lives in the
# adapters: a bot-triggered summon's reply does not re-trigger any bot. Guard 2
# is the exception, and it lives HERE because it needs one arbiter — plink's
# #1625 third-party option. Both residents' adapters spend against this one
# counter, so a review -> revision -> fix loop cannot buy itself twice the
# rounds by alternating who asks.
#
# Two ceilings, and they are NOT the same ceiling twice:
#
#   hop_cap (8)        ~4 review/fix round-trips, the 08-21 churn ceiling. At
#                      the cap the work item PARKS FOR A HUMAN: every further
#                      bot-to-bot hop on it is refused until a human posts in
#                      #custodian about it, which resets this counter to 0.
#   daily_hop_cap (24) hops on one work item in one UTC day, RESETS INCLUDED.
#                      The unpark is a report from an adapter — nothing else
#                      watches the channel — so this is what bounds that trust:
#                      repeated nudges, real or invented, cannot compound into
#                      an all-day burn.
#
# THE CLOCK NEVER UNPARKS ANYTHING (Claudette #1811). Midnight rolls the DAY
# counter and only the day counter; a chain parked at 23:59 is still parked at
# 00:01 and stays parked until a human has looked. A ledger that reset both at
# midnight would turn "parked for a human" into "parked until tomorrow", which
# is the same sentence with the human removed.
# --------------------------------------------------------------------------

DEFAULT_HOP_CAP = 8
DEFAULT_DAILY_HOP_CAP = 24


def format_hop_refusal(*, work_item: str, count: int, cap: int,
                       daily: bool = False) -> str:
    """The refusal line, fixed format (Claudette #1811).

    Broker-attributed, in-channel, and it names all three things the summoner
    needs: WHICH work item, HOW far it has gone, and WHAT would let it resume.
    A refusal missing the last one is a wall with no door, and the summoner
    retries against it.
    """
    if daily:
        return (f"summon refused: {work_item} at {count}/{cap} bot hops today "
                f"— the daily ceiling, which clears at 00:00 UTC")
    return (f"summon refused: {work_item} at {count}/{cap} bot hops "
            f"— parked until a human posts on it")


class HopLedger:
    """The per-work-item hop counter, persisted and restart-proof.

    State file shape::

        {"<work item>": {"hops": 8, "day": "2026-08-24", "day_hops": 11,
                         "unpark_seq": 1811}}

    ``hops`` is the parkable counter and survives every rollover; ``day_hops``
    is the daily ceiling and is the ONLY field the date touches.
    """

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
        """Charge one hop. Returns the decision; never raises."""
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
        """A human posted on this work item: the chain resumes at 0/cap.

        Idempotent per seq, because BOTH adapters see the same post and both
        report it; without this the second report would be a second reset and
        the day ceiling would be the only counter left doing any work.
        """
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
        planroom_api: Optional[Callable[..., dict]] = None,
    ) -> None:
        self.config = config
        self.verbs_path = verbs_path
        self.transport = transport or _sdk_transport
        # How the board verbs reach the Disjorn server's /planroom surface.
        # Injected in tests, exactly like `transport`.
        self.planroom_api = planroom_api or _planroom_http
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
        # Plan Room. `index` is the derived card cache this daemon WRITES and
        # the server reads; everything else here is about when to rebuild it.
        # Absent config means the board is simply not wired up on this host:
        # the verbs still work (they ask the server, which will say the index
        # is unavailable — an honest answer), and nothing rebuilds.
        self.planroom: dict[str, Any] = (
            config.get("planroom", {})
            if isinstance(config.get("planroom"), dict) else {})
        self._planroom_lock = threading.Lock()
        self._planroom_thread: Optional[threading.Thread] = None
        # Daily per-resident action budget (WP-H12). Loaded at construction;
        # a cap change needs a broker restart (unlike verbs.toml kill switches,
        # which are re-read live). Default: no cap == OFF. Instrument first.
        self.budgets: dict[str, Any] = config.get("budgets", {})
        # start-build (WP-L4) config: the detached build-session launch contract
        # (command + session_argv + model pin), the SPECS/ dir the confirm gate
        # reads, the wall-clock cap, and the per-day build budget.
        self.start_build: dict[str, Any] = config.get("start_build", {})
        # The bot-to-bot hop wall (2026-08-24). Absent section = no wall = the
        # summon-hop verb answers "no bucket" to everything, which is rule 1
        # and is exactly today's behaviour. Present section without a
        # state_path is config drift and fatal: a counter that cannot persist
        # would unpark every parked chain on every broker restart, which is the
        # one thing the human gate exists to prevent.
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
        # BR-1 (2026-08-14): the build identity is derived from the CALLER —
        # build_identity_from_caller — and [start_build].resident is dead. Warn
        # rather than ignore silently: a config line that still parses but no
        # longer does anything is how "the ratified default 2" happened, and the
        # next reader deserves to learn it is dead from the log, not from an
        # afternoon of tracing why edits to it change nothing.
        if "resident" in self.start_build:
            print("disjorn-broker: WARNING [start_build].resident is IGNORED "
                  "since BR-1 (2026-08-14): builds run as the resident that "
                  "CALLS start-build (SO_PEERCRED), never as a configured "
                  "name. Delete the line from broker.toml.", file=sys.stderr)
        # BL-D1: the confirm gate's REAL authorization is that specs_dir is
        # resident-unwritable. Verified HERE, once, at startup — a violation
        # raises ConfigError and main() exits non-zero, so the broker never
        # comes up with a forgeable confirm gate. A config with no [start_build]
        # section at all is not checked: start-build then fails closed at
        # request time (_specs_dir raises internal), so there is nothing to
        # forge. Presence of the section means someone intends to run builds,
        # and then specs_dir is mandatory and audited.
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
        # Wake (2026-08-25). Absent section = no wake surface at all: the verb
        # exists, every caller is refused as not-a-waker, and the refusal is
        # audited. Present section = plink means to wake seats, and then every
        # field below is mandatory and checked here, once, loudly.
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
        # Build-budget lock (H13-D4): count-with-reservation is held under this,
        # so two concurrent start-builds can NEVER both slip past the cap — the
        # check-then-act race the red-team flagged is closed here.
        self._build_lock = threading.Lock()
        # Wake-budget lock: the day's count is read from the spool and the new
        # record is written under this one lock, so two wakes pressed at once
        # cannot both read the same pre-cap count. The spool IS the ledger here
        # — there is no in-memory reservation to drift from it.
        self._wake_lock = threading.Lock()
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
            "summon-hop": self._verb_summon_hop,
            # The one verb no seat may call: dispatch refuses it to every
            # resident identity before verbs.toml is even read, and refuses
            # every other verb to a wake caller. See _check_wake_identity.
            WAKE_VERB: self._verb_wake,
            # Plan Room (SPECS/2026-08-20-plan-room.md). Three read, two write.
            # The two writes touch BOARD-NATIVE STATE ONLY — comments and the
            # blocked flag — and they are structurally unable to touch anything
            # derived, because derived state has no write path anywhere in this
            # house (seq 1428 P1). Not a check here: an absence there.
            "board-list": self._verb_board_list,
            "board-card": self._verb_board_card,
            "board-search": self._verb_board_search,
            "board-flag": self._verb_board_flag,
            "board-comment": self._verb_board_comment,
        }

    # -------------------------------------------------------- wake config

    def _parse_wake_callers(self) -> frozenset[str]:
        """`[wake].callers` — the identities that may wake a seat.

        Two refusals, both at startup and both fatal, because a wake surface
        that looks armed and is not is worse than one that is down:

        * a caller with no `[uids]` line can never be authenticated, so listing
          it grants nothing while reading as a grant;
        * a caller that is a SEAT is a self-wake with extra steps. The spec's
          wall is that origin arrives as connection data from a human's uid;
          a res-* name here would delete it in one config line."""
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
        """`[wake].residents` — the seats that may BE woken. Config, never
        caller input: the verb's `resident` argument is checked against this,
        so a wake can only ever reach a seat plink has named here."""
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

        # Wake identity, BEFORE the kill switch and before any handler: the
        # wake verb is the one verb whose caller is not a seat, and the two
        # halves of that are enforced here rather than left to verbs.toml.
        # A misedit there can widen a verb; it can never hand a resident the
        # wake, and it can never hand a waker anything else.
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

    # ------------------------------------------------- the gatehouse fetch
    # SPECS/2026-08-14-file-vision.md item 1. `refresh-mirror` used to move
    # `main` and nothing else, so the mirror could tell a resident what
    # production runs and could not show them a single branch anyone was being
    # asked to review. Every branch now lands under refs/gatehouse/<repo>/*.
    #
    # WHY TWO SOURCES AND NOT ONE (decision point, RESOLVED two-source by plink
    # 2026-08-15, and Claudette's second reason is the sharper one): `main` in
    # the mirror must equal the main production ACTUALLY RUNS, which is plink's
    # working clone — push-back to the gatehouse can lag a merge. Pointing
    # everything at the gatehouse would make mirror-main LEAD prod: the
    # merged-is-not-deployed gap inverted, in the direction nobody watches.
    #
    # NAMESPACES ARE DISJOINT AND THAT IS THE POINT. refs/remotes/origin/* is
    # incidental — the default refspec drags it along. refs/gatehouse/* is
    # deliberate, pruned, and named after the thing it mirrors.
    #
    # ON BRANCH-HIDING, STATED PLAINLY: nothing is being surrendered here. The
    # mirror ALREADY leaked a partial, stale, unpruned branch view (origin/loop/*
    # and even origin/worktree-agent-* ride the default fetch refspec). This
    # replaces accidental partial vision with deliberate complete vision.
    _GATEHOUSE_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def _gatehouse_fetch_argvs(self) -> list[tuple[str, list[str]]]:
        """One fixed argv per entitled gatehouse repo. Zero caller input reaches
        any of it: the repo list and the gatehouse directory are config, and the
        refspec is a constant. A resident can refresh, and can never aim git.

        The repo NAME is re-validated even though it is plink's config, on the
        same reasoning disjorn-build-launch gives for re-validating its slug:
        the cost is one regex and the failure it prevents is a config typo
        becoming a git flag."""
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

    @staticmethod
    def _parse_fetch_refs(output: str) -> tuple[list[str], list[str]]:
        """(arrived, vanished) ref names out of `git fetch --prune` chatter.

        A VANISHED ref is the interesting one and it is deliberately NOT
        interpreted: the branch was either harvested into main and deleted, or
        deleted without being harvested, and the mirror cannot tell those apart
        from here. The banner says "harvested or deleted" and names the ref, so
        a reader knows exactly what to go and check rather than being told a
        guess."""
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
        """Run every gatehouse fetch; return one record per repo. Raises
        exec-failure on the first failure — a mirror that is half-refreshed and
        says it succeeded is worse than one that says it did not."""
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
                            "vanished": vanished})
        return records

    def _verb_refresh_mirror(self, resident: str, args: dict) -> tuple[dict, str]:
        """Fast-forward the shared read-only repo mirror to the canonical
        repo's main, THEN re-fetch every entitled gatehouse repo's branches
        into refs/gatehouse/<repo>/*. The mirror is the ONLY view of the repo
        residents have (bind-mounted RO into each container), and nothing else
        ever fetches into it — host commits don't cross the wall until this
        runs. Zero caller args; every argv is fixed config, so a resident can
        refresh the mirror but can never aim git anywhere else. `--ff-only` on
        the main update: a diverged mirror fails loudly and stays plink's to
        resolve. `--prune` on the gatehouse fetches: a branch that vanished
        from the gatehouse vanishes here too, and the summary names it."""
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
        # The mirror has just moved, so every card derived from it may have
        # moved with it. Rebuilding HERE is the first of the Plan Room's three
        # triggers (seq 1428 P4). Best-effort by construction: a refresh that
        # fetched everything correctly and then failed to rewrite a cache has
        # still refreshed the mirror, and saying otherwise would teach
        # residents that a red refresh-mirror means nothing.
        planroom = ({"rebuilt": False, "reason": "disabled by config"}
                    if not self.planroom.get("rebuild_on_refresh", True)
                    else self._planroom_rebuild("refresh-mirror"))
        head = _head()
        summary = f"mirror at {head}" + ("" if head == before
                                         else f" (was {before})")
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
        """Fetch origin into the read-only mirror and fast-forward it to
        origin/main — the two fixed argvs `refresh-mirror` has always run,
        factored so the spec-status stamp can use the SAME refresh (never a
        second implementation of "the mirror is fresh"). Raises VerbError."""
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
        """(repo path, branch, SPECS subdir) of the CANONICAL repo whose SPECS/
        the mirror follows, from `[start_build].spec_repo` (+ `spec_repo_branch`,
        default main; `spec_repo_subdir`, default SPECS). None when unset —
        stamping is then off and every banner says so."""
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
        """One git command against the canonical repo, fixed argv, no shell.
        `commands.spec_repo_git` may replace the git binary (tests)."""
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
    #
    # THE PUSH LOG'S SIBLING (spec 2026-08-27, confirmed seq 2067). A stamp
    # commit is made with git plumbing straight onto the canonical repo's
    # branch: no push, so it never meets the pre-receive hook, so it can never
    # have a push-log line. The daily digest used to report exactly that as an
    # uncovered commit and blame a hook fault it had never measured, on the
    # same morning its own liveness line said that hook MATCHED. Five such
    # lines on 08-26, two on 08-24, growing by one every build and every
    # keyboard session: the shape of an alarm nobody will read.
    #
    # So the actor writes the record. This daemon is the one process that KNOWS
    # a local commit happened, at the instant it happens, and a positive record
    # naming the sha costs one appended line.

    LOCAL_LOG_NAME = "disjorn-local-log"
    LOCAL_STAMP = "local-stamp"

    def _local_coverage_log(self) -> Optional[str]:
        """Where the record goes: beside the push log, `[gate].local_log`,
        defaulting to <[gate].canonical_repo>/hooks/disjorn-local-log.

        PINNED ON BOTH SIDES, the same way DISJORN_PUSH_LOG is: metrics.py's
        `gate_paths` resolves these two keys by this rule and no other. Move
        one without the other and this daemon writes records nobody reads while
        the digest goes on calling its own stamps unexplained. There is a test
        that reads the resolution out of metrics and compares."""
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
        """Append `LOCAL <ts> <sha> <outcome>`. Returns "" or why it did not.

        NEVER RAISES, on the hook's fail-open reasoning inverted: the commit
        has already landed, and a coverage record that cannot be written must
        not turn a stamp that worked into a stamp that reports failure. It
        hands back a sentence, the caller carries it into the banner, and the
        next digest reports the commit as UNEXPLAINED — loud, and the correct
        answer for a record that was never written.

        O_APPEND, one whole line, exactly the hook's writer: two writers
        interleave as two lines rather than corrupting one."""
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
        """Move a spec's `## Status` line in the CANONICAL repo and commit it,
        then fast-forward the read-only mirror so residents (and this broker's
        own confirm gate) read the new word at once. Never raises: returns
        {ok, status, commit, why} and the caller narrates it.

        WHY THE BROKER WRITES SPECS/ AT ALL. "State lives in the file"
        (SPECS/README.md): a spec moves draft -> confirmed -> building ->
        built@<branch> -> merged, and the next resident reads the FILE, never
        chat scrollback. Nothing ever wrote the middle words. So a spec under
        construction still said `confirmed`, the board listed it as buildable,
        and on 2026-08-17 a resident set out to build one that another build
        had already claimed. The broker is the one process that KNOWS the
        transition the instant it happens — it launched the build — so it
        stamps the word; `board --mark-merged` stamps the last one when the
        merge lands. Same shape as the board's own reasoning: the thing that
        computes the truth writes it, and nobody has to remember.

        WHY A COMMIT ON THE CANONICAL REPO, NOT AN EDIT OF THE MIRROR FILE.
        The mirror is a fast-forward follower of the canonical repo's main; a
        dirty file in it makes the very next refresh refuse to merge the commit
        that touches that spec (mark-merged does, every cycle), and a
        broker-owned overlay that survives refreshes needs a second mechanism
        to re-derive it. Committing to the source and letting the existing
        refresh carry it keeps ONE truth with ONE reader.

        HOW, without touching the keyboard's working tree: plumbing against
        refs/heads/<branch> — read the blob at <branch>:SPECS/<slug>.md, rewrite
        the Status line, hash-object, build a tree in a THROWAWAY index
        (GIT_INDEX_FILE), commit-tree, then update-ref with the old sha as a
        compare-and-swap. The keyboard may be on any branch, mid-anything: its
        index and worktree are never read or written — EXCEPT one courtesy:
        when HEAD is that branch and the file is clean, `checkout HEAD -- path`
        syncs the worktree so `git status` stays quiet. A dirty file is left
        alone and named in the result.

        AND IT LEAVES A COVERAGE RECORD (seq 2067). The commit never meets
        the pre-receive hook — there is no push — so `_record_local_commit`
        appends one `local-stamp` line naming the sha, and the daily digest
        classifies it from that record instead of guessing at a hook state it
        never measured. A record that cannot be written is a sentence in
        `why`, never a failed stamp.

        WHAT A RESIDENT CONTROLS: nothing here. The slug is the gate-validated
        filename; the words written are this function's own; the one
        resident-influenced string (a failure reason) goes through
        _status_comment_text. `expect` guards the transition: the file must
        currently carry one of those words, else the stamp is refused — a
        keyboard that already advanced the spec is never overwritten."""
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
        # The coverage record comes FIRST of everything after the ref moved: it
        # names a commit that already exists, and the two steps below it are a
        # courtesy and a propagation, either of which can take its time or fail.
        note = self._record_local_commit(commit)
        if note:
            notes.append(note)
        # Courtesy sync of the keyboard's worktree, only when it is provably
        # safe: HEAD is this branch and the file has no local edits.
        try:
            head = self._git(repo, "symbolic-ref", "--quiet", "HEAD").stdout.strip()
            if head == ref:
                # "Clean" = worktree AND index still equal the commit we just
                # moved past (old_sha), not HEAD — HEAD is already the new
                # commit, against which an untouched checkout looks modified.
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

    def _build_argv(self, slug: str, build_resident: str) -> list[str]:
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

    @staticmethod
    def _read_build_head(path: str, limit: int = MAX_BUILD_LOG_TAIL) -> str:
        """The FIRST `limit` bytes, truncated at the last complete line.

        Only the quarantine notices need this: provisioning prints QUARANTINED
        before the session runs, so on a chatty build those lines are tens of
        megabytes above the tail the reaper reads — and a quarantined clone that
        nobody is told about is the exact failure the quarantine clause exists to
        prevent. Bounded the same way as the tail (one `limit` per file, so the
        reaper's ceiling is 2x MAX_BUILD_LOG_TAIL per build), and the trailing
        partial line is dropped so a half-written sha can never be quoted as a
        measurement."""
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
        """The wrapper's publish lines for one build: parsed from the log's head
        AND tail, because the two ends carry different halves of the protocol
        (quarantine at provisioning time, verdicts after the container exits)."""
        return _parse_publish_lines(self._read_build_head(out_path) + "\n"
                                    + out_tail)

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

    def _stop_build_unit(self, slug: str, build_resident: str) -> bool:
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
            cp = self._run([*argv, build_resident, slug], 60)
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
            # Since BR-1 these two agree by construction — `build_resident` is
            # DERIVED from `caller` (strip res-, launch helper re-derives uid/
            # home/config from it). Both are still recorded: their equality is
            # now an invariant a reader can CHECK, and the day they differ the
            # sidecar is the evidence of what broke.
            "caller": meta.get("resident"),
            "build_resident": meta.get("build_resident", ""),
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

    def _refresh_mirror_for_banner(self, branch: str, published) -> str:
        """SPEC ITEM 5 — refresh the mirror BEFORE the banner that names a sha.

        WHERE THIS LIVES, AND WHY IT IS NOT THE WRAPPER. The spec's wording is
        "the wrapper runs the same mirror fetch host-side before the reaper
        banners". It cannot: run-build.sh runs under `systemd-run --uid=res-<r>`
        and the res-* uid CANNOT WRITE /srv/disjorn-ro — that is one of the
        resident->host walls this house has actually verified (AUTHORITY-PLAN.md
        "verified this session that res-* cannot write /srv/disjorn-ro"), the
        wrapper holds no sudo, and it mounts the mirror :ro. So the fetch runs
        in the one process that already owns the mirror argvs and already runs
        as plink: this reaper, on the last line before the banner. The spec's
        INVARIANT — "a banner may never name a sha the audience cannot open" —
        is met exactly; only the hand that does it moved. FLAGGED FOR REVIEW as
        the single deviation in this build.

        THE SAME FETCH, not a second implementation: it calls the identical
        argv builder `refresh-mirror` uses, so the two can never drift into
        disagreeing about what "the mirror is fresh" means.

        Best-effort by construction. A fetch failure must not swallow a build's
        banner — the banner is the only thing anyone hears — so the error is
        carried INTO the banner text instead of raised."""
        if not published:
            return ""
        error = None
        try:
            if not self._gatehouse_fetch_argvs():
                # No gatehouse configured: there is no mirror claim to make, so
                # the banner makes none. An unmigrated broker.toml gets the
                # 08-13 banner unchanged rather than a line about a refresh
                # that never happened.
                return ""
            self._fetch_gatehouse_into_mirror(
                SUBPROCESS_TIMEOUTS["refresh-mirror"])
        except VerbError as exc:
            error = exc.message
        except Exception as exc:  # noqa: BLE001 — never crash a reaper
            error = repr(exc)
        return format_mirror_note(branch, published, error)

    def _narrate_build_outcome(self, **kwargs) -> None:
        """Every terminal build banner goes through here, so the mirror fetch
        cannot be forgotten on one of the five paths that post one."""
        publish = kwargs.get("publish") or {}
        kwargs["mirror"] = self._refresh_mirror_for_banner(
            kwargs.get("branch", ""), publish.get("published", []))
        # The spec's Status line moves with the banner — `built@<branch>`,
        # `failed`, or back to `confirmed` — from the SAME ladder the banner is
        # narrated from (build_outcome_class), so file and banner cannot tell
        # two stories. Only from `building`: a keyboard that already moved the
        # word (merged it by hand, superseded it) is never overwritten.
        status, comment = spec_status_after_build(
            branch=kwargs.get("branch", ""), publish=publish,
            unit_reason=kwargs.get("unit_reason"))
        stamp = self._stamp_spec_status(kwargs.get("slug", ""), status, comment,
                                        expect=("building",))
        self._narrate(format_build_outcome(**kwargs)
                      + format_spec_status_note(stamp))
        # A build's terminal banner is the loudest column transition the house
        # has — `building` -> Review, or `building` -> back to Ready on a
        # failure — and the mirror was just refreshed two lines above. This is
        # the Plan Room's second rebuild trigger (seq 1428 P4): the board moves
        # WITH the build rather than a quarter-hour behind it. It posts its own
        # transition lines only if the columns actually changed, so a build
        # never costs #custodian two banners about the same event unless two
        # things really happened.
        self._planroom_rebuild("build-outcome")

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
        stops the UNIT first and only then reaps the local process.

        2026-08-13 (publish path): the terminal banner is derived from the
        wrapper's PUBLISHED / PUBLISH-FAILED / NO-COMMITS / QUARANTINED lines in
        that spool, through format_build_outcome. The reaper runs NO
        verification of its own — no rev-parse, no second look at the gatehouse.
        The harvest is the verification; a second mechanism could only ever
        disagree with it.

        2026-08-14 (file vision, item 5): it does now run ONE git command, and
        the distinction matters. `_narrate_build_outcome` fetches the gatehouse
        into the read-only mirror before posting. That is a PUBLICATION step,
        not a verification step: it measures nothing, decides nothing, and
        cannot change the banner's verdict — it only makes the sha the banner
        names openable by the people being asked to review it. The one-mechanism
        rule above is intact."""
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
                # A killed wrapper never harvests (by design), so there are no
                # verdict lines to find — but provisioning's QUARANTINE notices
                # are already in the log, and this is exactly the run whose work
                # is sitting in a quarantine directory. Route through the same
                # decision so they are appended here too.
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
            # report is stripped out of their way and demoted to enrichment
            # (08-13 spec item 3). A nonzero exit is still a failure, but it no
            # longer decides ALONE: the harvest may have published before the
            # wrapper exited nonzero, and that must be named too.
            publish = self._harvest_report(out_path, out_s)
            session_out = _strip_publish_lines(out_s)
            report = _parse_build_report(session_out)
            rc = getattr(proc, "returncode", None)

            # PREFLIGHT REFUSAL (exit 78, EX_CONFIG). The wrapper checked the
            # image before starting anything and found it unable to import a
            # stack the repo's tests need. Nothing ran: no container, no clone
            # touched by a session, no commits, no harvest.
            #
            # So refund the slot. BL-D3's rule is that a build which never
            # started must not cost an attempt, and this is the purest case of
            # it — the seat was unfit before the session existed. A build that
            # ran and then failed still burns its slot; that distinction is the
            # whole reason this is keyed to one specific exit code rather than
            # to "nonzero".
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
        """The done/failed line for a build this process did not launch.

        There is no exit status to read: `--collect` unloads the unit when it
        ends, and systemd cannot tell us about a unit it has forgotten. The
        EVIDENCE is therefore the same evidence the live reaper uses — the
        wrapper's publish lines in the spool (08-13 spec item 3). It used to be
        the session's JSON report, which said what the session BELIEVED it had
        done; the harvest lines say what actually reached the gatehouse, and
        both reapers must derive the same banner from them or this process's
        restart would change a build's story.

        A build that left no publish lines is still narrated loudly rather than
        guessed at: vanished mid-flight and failed are the same thing to a
        reviewer, and neither one published anything."""
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
          2b. if a wake is in flight for this seat, the woken session's
             no-self-review rule (_assert_woken_build_allowed);
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

        # 2b. If this seat is awake on a wake right now, the woken session's
        #    extra rule applies: no build whose review owner is this seat (or
        #    the seat that woke it). The confirm gate above is what makes the
        #    inherited verb safe at all; this is what keeps waking from being a
        #    route around the review owner.
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

        # The spec's Status line moves to `building` NOW — before the started
        # line and before the spawn — so the file (the state of record) never
        # says `confirmed` about a build that is under way, and so the reaper
        # thread, which may finish in milliseconds under test, always finds
        # `building` when it comes to stamp the terminal word. Best-effort:
        # the result rides on the started line, and a failed stamp is said
        # there out loud, never swallowed.
        stamp = self._stamp_spec_status(
            meta["slug"], "building",
            f"build running as {build_unit_name(meta['slug'])} -> {meta['branch']}, "
            f"launched by {build_resident} (confirmed by {meta['confirmed_by']}, "
            f"#custodian seq {meta['seq']}). Not buildable again until this "
            "line moves.",
            expect=("confirmed",))

        # 'started' — a state transition; best-effort (a failed post must never
        # sink a launched build, and is never a heartbeat).
        self._narrate(format_build_started(
            slug=meta["slug"], branch=meta["branch"],
            confirmed_by=meta["confirmed_by"], seq=meta["seq"], eta_sec=timeout)
            + format_spec_status_note(stamp))

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
                  # The transient unit the build runs in. Derivable from the
                  # slug, surfaced anyway: it is the one string that makes a
                  # running build inspectable (`systemctl status <unit>`), and
                  # `pid` alone is now the LOCAL sudo/systemd-run process, not
                  # the build.
                  "unit": build_unit_name(meta["slug"]),
                  "confirmed_by": meta["confirmed_by"], "seq": meta["seq"],
                  # What happened to the spec's Status line (-> `building`).
                  # ok=False is NOT a refusal — the build runs regardless —
                  # but the caller should say so where a human will read it.
                  "spec_status": stamp}
        budget_str = f"{used}/{cap}" if cap is not None else str(used)
        # Third element = audit extras. `build_started` is the BL-D3 marker:
        # the ONLY place it is emitted is here, after a successful spawn, so
        # the audit log distinguishes "this build ran" from "this call was
        # authorized but never launched a thing".
        return (result,
                f"build {meta['slug']} -> {meta['branch']} launched "
                f"(budget {budget_str})",
                {"build_started": True})

    # ---------------------------------------------------------------- wake

    def _check_wake_identity(self, resident: str, verb: str) -> Optional[str]:
        """None if this identity may attempt this verb, else the refusal text.

        Two rules, symmetric, both in code rather than in verbs.toml:

        * only a configured wake caller may call `wake`, so a wake from any
          resident, build or adapter uid is refused and audit-logged. The
          caller arrives as an SO_PEERCRED uid, so this is the sentence that
          makes "no text in any channel can constitute a wake" true;
        * a wake caller may call nothing ELSE. The identity exists to press one
          button; plink has better routes to every other verb than a socket
          that answers as the broker.

        With no `[wake]` section there are no callers, so the first rule
        refuses everyone: the verb is present and inert, which is the same
        fail-closed shape as an unflipped kill switch."""
        is_waker = resident in self.wake_callers
        if verb == WAKE_VERB and not is_waker:
            return (f"{resident} may not wake anyone — the wake caller is "
                    "authenticated by uid at the socket, and no seat is one")
        if is_waker and verb != WAKE_VERB:
            return (f"{resident} is a wake caller and may call only "
                    f"{WAKE_VERB!r}")
        return None

    def _wake_spool_dir(self) -> str:
        """The spool realpath VERIFIED resident-unwritable at construction —
        never the raw config string, for the reason _specs_dir prefers its
        verified path: the directory written must be the directory proven."""
        if not self.wake_spool_real:
            raise VerbError("internal", "wake.spool_dir is not configured")
        return self.wake_spool_real

    def _write_wake_record(self, record: dict) -> str:
        """One 0644 JSON record per wake, written atomically.

        Atomic because the seat's runner POLLS this directory: a reader that
        catches a half-written file would see a wake with no task, and the
        rename means it either sees the whole record or no file at all. 0644
        because the runner must read it and must not write it."""
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
        """Every parseable record in the spool. A record the broker cannot
        parse is skipped rather than fatal: the spool is plink-owned, so a bad
        file is an operator's mistake, and refusing every future wake over it
        would be the wrong blast radius."""
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
        """When the wake was asked for. None when the record carries no
        readable time — such a record is treated as neither in flight nor
        worth keeping, never as forever-live."""
        try:
            started = _dt.datetime.fromisoformat(str(record.get("requested_at")))
        except (TypeError, ValueError):
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=_dt.timezone.utc)
        return started.timestamp()

    @classmethod
    def _wake_window_ends(cls, record: dict) -> Optional[float]:
        """When this wake stops being in flight: requested_at + the session cap
        + the grace margin."""
        started = cls._wake_requested_epoch(record)
        if started is None:
            return None
        cap = record.get("session_cap_sec")
        grace = record.get("grace_sec")
        cap = cap if isinstance(cap, int) and cap > 0 else DEFAULT_WAKE_SESSION_CAP_SEC
        grace = grace if isinstance(grace, int) and grace >= 0 else DEFAULT_WAKE_GRACE_SEC
        return started + cap + grace

    def _prune_wake_spool(self, now: Optional[float] = None) -> int:
        """Delete records past the RETENTION horizon (not past their window).
        Runs on each wake, so the spool cannot grow without bound, while a
        recently-expired record survives long enough for a runner that was down
        to find it and post that the wake was missed."""
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
        """`[wake].daily_wake_cap`, else DEFAULT_DAILY_WAKE_CAP.

        A non-int reads as the default rather than as no cap: config drift must
        never be the thing that removes a wall."""
        cap = self.wake.get("daily_wake_cap", DEFAULT_DAILY_WAKE_CAP)
        if isinstance(cap, bool) or not isinstance(cap, int):
            return DEFAULT_DAILY_WAKE_CAP
        return cap

    def _wake_spend_today(self, seat: str, now: float) -> tuple[int, float]:
        """(wakes, session seconds) recorded for this seat so far today (UTC).

        Counted from the spool rather than the audit log, because the record is
        written under the same lock as this count — the two cannot disagree the
        way a build's audit-log seeding can, and the spool's week of retention
        covers any day this asks about.

        THE SECONDS ARE A CEILING, not a measurement. From here the broker knows
        when a wake started and what wall clock it granted the runner; it never
        learns when the session actually stopped (the runner posts that). So a
        wake counts for what it was granted, bounded by how much of that has
        elapsed — an in-flight wake grows toward its cap instead of claiming it
        up front."""
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
        """The most recent wake still in flight for this seat, if any.

        Read from the spool per request, not from memory, so a broker restart
        mid-wake does not lose the fact that a seat is awake.

        NOTE what this can and cannot tell apart. From the socket, a call by a
        woken session and a call by a summoned one are the same uid; the window
        is the only signal there is. So while a wake is in flight for a seat,
        EVERY build that seat starts is held to the woken session's rules. That
        is the safe direction of the imprecision — it can refuse a build a
        summon could have run, and it can never let a woken session past a rule
        by mistaking it for a summon."""
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
        """The no-self-review rule, one level down (2026-08-25 spec).

        A woken session inherits `start-build` from the summon seat, and that
        is only safe while the confirm gate stays upstream of every build. This
        is the other half: waking must not become a route to a build the seat
        would then review itself.

        A spec that states NO review owner is refused, not waved through: the
        rule is a comparison, and a comparison with nothing to compare cannot
        be satisfied. A spec that names a human refuses nothing — that is the
        case the rule protects. The second clause (the waker's own seat) is
        inert while only humans wake, and is written now so that it is already
        true on the day a non-human wake lands."""
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
        """Wake a seat with a task (SPECS/2026-08-25-agentic-residents.md).

        The broker's whole part is authentication and a record: it resolves the
        caller from SO_PEERCRED (dispatch has already refused every identity
        but a configured waker), checks the named seat against config, and
        drops one record in the plink-owned spool. It launches nothing — the
        session runs in the seat's own container under the seat's own uid, and
        the seat's runner is what launches, caps, harvests and posts it.

        The caps ride ON the record rather than living in the runner's config,
        so the wall-clock a woken session runs against is plink-owned and
        singular. Widening it is a witnessed edit to broker.toml.

        The DAILY cap is enforced here and nowhere else: the seat's runner sees
        one record at a time and cannot count a day, and a wake refused here
        never becomes a record, so nothing downstream has to know about it."""
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
            # A FACT field, like start-build's `build_started`: the wake id is
            # what ties this line to the action log's start/end pair and to the
            # #custodian post the seat's runner makes.
            {"wake_id": record["wake_id"]},
        )

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

    # ------------------------------------------------------- summon hops

    def _work_item_bucket(self, work_item: Optional[str]) -> Optional[str]:
        """The work item this chain spends against, or None for no bucket.

        A chain may continue past depth 1 only on a LIVE work item: a card the
        board knows, sitting in Review. Chat data selects the bucket — a slug
        in the summoning message — and plink-owned config owns the wall, so
        naming a slug buys a bucket and nothing else. A slug the board does not
        know, a card in any other column, or a board that cannot be reached at
        all: no bucket, and rule 1 applies.
        """
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
        """The bot-to-bot hop wall, for the summon adapters (2026-08-24).

        `spend` asks whether one bot-to-bot summon may continue the chain past
        depth 1; `unpark` reports the human post that resumes a parked one.
        Neither can widen anything: the caps are broker.toml's, the columns are
        the board's, and the only thing an argument decides is WHICH bucket is
        charged. The answer `chain: false` is not a refusal — it means "serve
        this summon, but your reply must not re-trigger anyone", which is the
        depth-1 default every summon has run under since WP-H9.
        """
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
    #
    # Five verbs. Three read, two write, and the two writes touch BOARD-NATIVE
    # STATE ONLY: comments and the blocked flag + its reason. They are
    # structurally unable to touch derived state — not because anything here
    # checks, but because derived state has no write path anywhere in this
    # house (seq 1428 P1). Nothing a resident can send through this socket can
    # move a card between columns; a card changes columns only because reality
    # moved. Phase II's write-through is a separate spec.

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
        """The board, ONE LINE PER CARD. Filters by column, lane, owner, blocked.

        Skim is the default and detail is opt-in — the context-budget answer to
        the tricky part of the request that started this feature: "so that your
        entire context window isn't swallowed by reading the whole thing all
        the time". `board-card` is where the whole thing lives."""
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
        """Block or unblock a card, with a reason. BOARD-NATIVE STATE ONLY.

        Blocked is a FLAG WITH A REASON, NEVER A COLUMN: the card does not move,
        so everyone can see where it re-enters. A reason is required to block —
        a card blocked for no stated reason is one nobody can unblock, because
        nobody can tell what would have to change.

        The resident's name is stamped HERE, from the broker's own
        SO_PEERCRED-derived identity, never from the caller's arguments. Same
        rule `file-proposal` has always run under: the resident supplies data,
        the broker supplies the authority and the attribution."""
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
        """Add a comment to a card. BOARD-NATIVE STATE ONLY.

        This and `board-flag` are what keeps "residents triage" a sentence about
        something residents can actually do."""
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
        """Re-derive the board and rewrite the index. BEST EFFORT, ALWAYS.

        Called from `refresh-mirror`, from a build's terminal banner, and from
        the daemon's own timer (seq 1428 P4) — a trigger nobody has to
        remember, so the index refreshes when `main` moves and not only when a
        resident happens to call a verb.

        It never raises and it never turns its caller's success into a failure.
        A refresh-mirror that fetched everything correctly and then failed to
        rebuild a cache has still refreshed the mirror; reporting otherwise
        would teach residents that a red refresh-mirror means nothing. The
        outcome is carried in the return value and lands in the audit line
        instead.

        Serialised under a lock: the timer and a verb can fire at the same
        moment, and two concurrent rebuilds racing on one temp file is how a
        cache becomes a corrupt file nobody can explain."""
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
            # ONE SYSTEM LINE PER COLUMN TRANSITION, NEVER PER EDIT. Residents
            # are event-driven, so this stream is their trigger; and because it
            # lands in #custodian it doubles as a witnessable seq trail of the
            # whole lifecycle, for free. A rebuild that moved nothing says
            # nothing — a detector that narrates every tick teaches everyone to
            # stop reading it.
            self._narrate("\n".join(lines[:20]))
        return {"rebuilt": True, "cards": len(data["cards"]),
                "transitions": len(lines), "why": why}

    def _planroom_timer(self) -> None:
        """The daemon's own rebuild tick.

        In the daemon rather than in a systemd unit on purpose: P4 asks for a
        trigger nobody has to remember, and this week's install record argues
        hard against trusting a hand step. A `.timer` file is one more thing
        that can be committed and not installed, and a second process writing
        the index is one more thing that can race the first."""
        interval = self.planroom.get("timer_sec", DEFAULT_PLANROOM_TIMER_SEC)
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            interval = DEFAULT_PLANROOM_TIMER_SEC
        if interval <= 0:
            return
        while not self._closed:
            # Sleep first: startup already rebuilds nothing in particular, and
            # a daemon that re-derives the whole repo the instant it comes up
            # makes a restart the most expensive thing on the host.
            slept = 0.0
            while slept < interval and not self._closed:
                time.sleep(min(1.0, interval - slept))
                slept += 1.0
            if self._closed:
                return
            try:
                self._planroom_rebuild("timer")
            except Exception:  # noqa: BLE001 — belt and braces; _planroom_rebuild
                # already swallows, and a dead timer thread is a board that
                # silently stops moving.
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
        # The Plan Room's third rebuild trigger (seq 1428 P4). Started here
        # rather than in __init__ so constructing a Broker — which tests and
        # tooling do — never spawns a thread that re-derives the whole repo.
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
