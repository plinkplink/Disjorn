#!/usr/bin/env python3
"""disjorn-broker — the privileged verb gateway for residents (WP-H3)."""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import pwd
import re
import shutil
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

# The gate runner is a sibling file, and brokerd runs as a script.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import gates  # noqa: E402

from broker_common import (  # noqa: E402,F401
    MAX_REQUEST_BYTES, BOARD_SLUG_RE, SUBPROCESS_TIMEOUTS, _DATE_RE,
    _SPEC_STEM_RE, BUILD_UNIT_PREFIX, BUILD_ACTIVE_STATES, SERVER_IDENTITY,
    BUILD_VERB, BUILD_REFUSED, MAX_SEQ, BOT_HIDDEN_FLAGS,
    DEFAULT_WAKE_SESSION_CAP_SEC, DEFAULT_WAKE_GRACE_SEC, build_unit_name,
    read_peer_cgroup, hidden_from_bots, VerbError, _bad, ConfigError,
    _resident_gids, _is_within, _path_components,
    assert_specs_dir_resident_unwritable, assert_dir_resident_unwritable,
    _check_int, _check_str, _reject_unknown, _split_diff_range, _safe_path,
    _parse_numstat, _parse_name_status, _check_date, _sdk_transport,
    _sdk_channel_transport, _clean_field, parse_spec_status, _BUILD_CALLER_RE,
)
from broker_planroom import (  # noqa: E402,F401
    MAX_BOARD_CARDS, MAX_BOARD_COMMENT_CHARS, MAX_BOARD_REASON_CHARS,
    MAX_BOARD_SEARCH_CHARS, PLANROOM_HTTP_TIMEOUT, MAX_APPROVAL_ROWS,
    MAX_APPROVAL_REMARKS_CHARS, APPROVAL_ACTIONS, BACKLOG_STATUSES,
    MAX_BACKLOG_LIST_ROWS, BACKLOG_TEXT_CLIP, BACKLOG_PAGE,
    BACKLOG_DAILY_FILE_CAP, format_approval_line, _api_label, _planroom_http,
    _urlq, format_board_line, _deploy_words, format_board_face, PlanroomVerbs,
)
from broker_wake import (  # noqa: E402,F401
    WAKE_VERB, MAX_WAKE_TASK_CHARS, WAKE_SPOOL_SUFFIX, WAKE_SPOOL_SCHEMA,
    WAKE_RETENTION_SEC, DEFAULT_DAILY_WAKE_CAP, _WAKE_ID_RE, new_wake_id,
    format_session_time, format_wake_refusal, parse_review_owner,
    review_owner_seat, DEFAULT_HOP_CAP, DEFAULT_DAILY_HOP_CAP,
    format_hop_refusal, HopLedger, WakeVerbs,
)
from broker_mirror import (  # noqa: E402,F401
    replace_spec_status, MirrorVerbs,
)
from broker_build import (  # noqa: E402,F401
    START_BUILD_DEFAULT_TIMEOUT, MAX_SPEC_BYTES, MAX_BUILD_LOG_TAIL,
    BUILD_SIDECAR_SUFFIX, BUILD_SIDECAR_SCHEMA, MAX_BUILD_TEXT_CHARS,
    BUILD_SLUG_WORDS, MAX_BUILD_SLUG_STEM, MAX_BUILD_SLUG_TRIES,
    _SLUG_WORD_RE, _PUBLISH_REPO_RE, _PUBLISH_LINE_RES, MAX_PUBLISH_LINES,
    MAX_PUBLISH_ERR_CHARS, MAX_QUARANTINE_PATH_CHARS, _status_comment_text,
    build_outcome_class, spec_status_after_build, parse_confirm_record,
    build_identity_from_caller, slug_from_spec_filename, build_session_prompt,
    strip_build_command, slug_from_build_text, build_chat_prompt,
    _FENCED_JSON_RE, _json_object_from_text, _parse_build_report,
    _match_publish_line, _parse_publish_lines, _strip_publish_lines,
    _publish_reported, _quarantine_suffix, format_build_started,
    format_build_done, format_seq_build_banner, format_build_refused,
    format_build_failed, NO_HARVEST_REASON, format_mirror_note,
    format_spec_status_note, format_build_outcome, BuildVerbs,
)
from broker_merge import (  # noqa: E402,F401
    MERGE_VERB, MERGE_REFUSED, MERGE_REASONS, GATE_UNIT_RUNTIME_CAP_SEC,
    DEFAULT_GATE_TIMEOUT_SEC, DEFAULT_GATE_LOG_DIR, DEFAULT_MERGE_WORK_DIR,
    DEFAULT_AUTO_APPLY_BUDGET, MERGE_IDENTITY_NAME, MERGE_IDENTITY_EMAIL,
    MAX_MERGE_PATHS, MAX_SLUG_CHARS, _PASS_WORD_RE, _BLOCK_WORD_RE,
    WRITE_VERB, WRITE_RECORD_HEADER, WRITE_RECORD_BEGIN, WRITE_RECORD_END,
    WRITE_RECORD_KEYS, DEFAULT_WRITE_FRESHNESS_SEC, MAX_WRITE_CONTENT_BYTES,
    _SHA256_RE, _REPO_PATH_COMPONENT_RE, check_repo_path, parse_write_record,
    tier_for_path, _as_utc, merge_commit_message, message_names_slug,
    format_merge_done, format_merge_refused, merge_next_step,
    format_gate_tests_line, format_tier_line, MergeVerbs, WriteWall,
)
from broker_apps import (  # noqa: E402,F401
    APPS_UNIT_PREFIX, APPS_SIDECAR_SUFFIX, APPS_SIDECAR_SCHEMA,
    APPS_LAUNCH_REFUSED_EXIT, APPS_SPAWN_CHECK_SEC, APPS_DETAIL_MAX_CHARS,
    APPS_FILES_CAP, APPS_SUMMARY_MAX, APPS_TURN_MAX_SEC, APPS_DEFAULTS,
    APPS_APP_ID_RE, APPS_LAUNCH_REFUSED, APPS_STOP_MAX_REFUSALS, APPS_SEAT_RE,
    _APPS_MORE_RE, APPS_CHAT_MARKER_REFUSAL, apps_unit_name, apps_tokens,
    apps_clean_line, apps_cap_files, apps_fit_detail, AppsVerbs,
)

DEFAULT_CONFIG_PATH = "/etc/disjorn-broker/broker.toml"
DEFAULT_VERBS_PATH = "/etc/disjorn-broker/verbs.toml"
ENV_CONFIG = "DISJORN_BROKER_CONFIG"
ENV_VERBS = "DISJORN_BROKER_VERBS"

DEFAULT_SOCKET_PATH = "/run/disjorn-broker.sock"  # per HARNESS-PLAN; the
# shipped broker.toml template uses /run/disjorn-broker/broker.sock instead so the
# daemon can run unprivileged under systemd RuntimeDirectory=.
MAX_PROPOSAL_CHARS = 4000
MAX_LOG_LINES = 500
MAX_AUDIT_ENTRIES = 500
MAX_GREP_CHARS = 200
MAX_GATES_JSON = 8192
# One `changed-files` answer has to fit a reviewer's context, so the file list
# is capped and the totals keep counting past the cap.
MAX_CHANGED_FILES = 500
# How often the daemon re-derives the board when nothing else has triggered it.
DEFAULT_PLANROOM_TIMER_SEC = 900
# Ratified default (BUILD-LOOP.md): builds are CAPPED by default (2/day), unlike the
# WP-H12 action budget which ships OFF. plink tunes at staging time.
DEFAULT_DAILY_BUILD_CAP = 2

_RANGE_RE = re.compile(r"^[A-Za-z0-9._~^/{}-]{1,200}$")  # git rev / range; no
# whitespace, no leading dash (checked separately) — can never be read as a flag.

# ------------------------------------------------------------- the server
# The Disjorn server runs under plink's own uid, so the peer uid alone cannot
# tell the two apart: the peer pid's cgroup must name [server].unit as well.
SERVER_PEER_IDENTITY = "plink"
DEFAULT_SERVER_UNIT = "disjorn.service"
# A chat build spends the SERVER's allowance, not the seat's.
DEFAULT_CHAT_BUILD_CAP = 4


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


# The wrapper's exit code for "this seat cannot run a test; nothing started".
PREFLIGHT_REFUSED_EXIT = 78


# --------------------------------------------------------------------------
# The broker.
# --------------------------------------------------------------------------

class Broker(
    PlanroomVerbs, WakeVerbs, MirrorVerbs, BuildVerbs, MergeVerbs, WriteWall,
    AppsVerbs,
):
    """Unix-socket verb broker. Construct with parsed broker.toml + a path to
    verbs.toml (re-read per request — that's the kill-switch property)."""

    def __init__(
        self,
        config: dict,
        verbs_path: str,
        *,
        transport: Optional[Callable[[dict, str], dict]] = None,
        channel_transport: Optional[Callable[[dict, int, str], dict]] = None,
        build_spawn: Optional[Callable[[list[str]], Any]] = None,
        planroom_api: Optional[Callable[..., dict]] = None,
        apps_spawn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = config
        self.verbs_path = verbs_path
        self.transport = transport or _sdk_transport
        # How a banner reaches the channel a `/build` was typed in.
        self.channel_transport = channel_transport or _sdk_channel_transport
        # How the server principal is told apart from plink at the keyboard.
        self._read_peer_cgroup: Callable[[int], str] = read_peer_cgroup
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
        # The server principal (spec 2026-09-20-build-lane-v2-stage1-2b).
        server_cfg = config.get("server")
        unit = ((server_cfg or {}).get("unit", DEFAULT_SERVER_UNIT)
                if isinstance(server_cfg, dict) else DEFAULT_SERVER_UNIT)
        if not isinstance(unit, str) or not unit.strip():
            raise ConfigError(
                "[server].unit must be a non-empty systemd unit name; refusing "
                "to start (an empty unit matches every cgroup, which would "
                "hand the server principal to anything running as plink)")
        self.server_unit: str = unit.strip()
        self.build_cfg: dict[str, Any] = config.get("build", {}) or {}
        self.build_humans: frozenset[str] = frozenset()
        self.build_seat: str = ""
        self.build_ledger: str = ""
        self.chat_build_cap: int = DEFAULT_CHAT_BUILD_CAP
        self.gate_timeout: int = DEFAULT_GATE_TIMEOUT_SEC
        self.gate_log_dir: str = DEFAULT_GATE_LOG_DIR
        self.merge_work_dir: str = DEFAULT_MERGE_WORK_DIR
        if self.build_cfg:
            self.build_humans = self._parse_build_humans()
            self.build_seat = self._parse_build_seat()
            self.build_ledger = self._parse_build_ledger()
            self.chat_build_cap = self._parse_build_int(
                "daily_build_cap", DEFAULT_CHAT_BUILD_CAP)
            self.gate_timeout = self._parse_build_int(
                "gate_timeout_sec", DEFAULT_GATE_TIMEOUT_SEC,
                minimum=GATE_UNIT_RUNTIME_CAP_SEC + 1,
                stake=f"the gate unit's own runtime cap is "
                      f"{GATE_UNIT_RUNTIME_CAP_SEC} seconds; the broker must "
                      f"outwait it")
            self.gate_log_dir = self._parse_build_dir(
                "gate_log_dir", DEFAULT_GATE_LOG_DIR)
            self.merge_work_dir = self._parse_build_dir(
                "merge_work_dir", DEFAULT_MERGE_WORK_DIR)
        # The fails-closed Tier-1 wall. An empty [write_verbs] is no write
        # surface (every caller refused and audited, like an unflipped switch);
        # a populated one is checked here, once, loudly.
        self.write_verbs: dict[str, Any] = config.get("write_verbs", {}) or {}
        self.write_seats: dict[str, dict] = (
            self._parse_write_seats() if self.write_verbs else {})
        self.write_consumed: str = (
            self._parse_write_consumed() if self.write_verbs else "")
        self._audit_lock = threading.Lock()
        # Held across reading the consumed-set and appending to it, so two
        # concurrent calls can never both spend one record.
        self._write_lock = threading.Lock()
        # Build-budget lock (H13-D4): count-with-reservation is held under this, so
        # two concurrent start-builds can NEVER both slip past the cap — the
        # check-then-act race the red-team flagged is closed here.
        self._build_lock = threading.Lock()
        # Merge lock: the Tier 0 budget is read and spent, and main is written,
        # under this one lock — two merges may never race onto main.
        self._merge_lock = threading.Lock()
        # One gate run per slug at a time, whether a `/merge` or a build's end
        # started it: a second run would gate a branch the first is merging.
        self._gate_lock = threading.Lock()
        self._gate_runs: set[str] = set()
        # Background merge threads, kept ONLY so tests can join them; production
        # never waits — the room hears the outcome as a post.
        self._merge_threads: list[threading.Thread] = []
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
        # Backlog filings per seat for the day, under the same reservation rule.
        self._backlog_lock = threading.Lock()
        self._backlog_files: dict[str, tuple[Optional[str], int]] = {}
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
            # The one verb whose caller is the server, not a seat.
            BUILD_VERB: self._verb_build,
            MERGE_VERB: self._verb_merge,
            "classify-diff": self._verb_classify_diff,
            "changed-files": self._verb_changed_files,
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
            # The approval object, over the server's /approval surface, so a
            # resident answers the same record plink answers in the modal.
            "approval-list": self._verb_approval_list,
            "approval-show": self._verb_approval_show,
            "approval-act": self._verb_approval_act,
            "backlog-list": self._verb_backlog_list,
            "backlog-file": self._verb_backlog_file,
            # The fails-closed Tier-1 wall: the only verb here that writes
            # outside the resident's own volume, and only on a #custodian record
            # the broker read itself.
            WRITE_VERB: self._verb_apply_posted_write,
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
        """Per-day build cap for a resident, or for the server principal.

        A chat build spends `[build].daily_build_cap` under `server` and NEVER
        the build seat's `[start_build]` allowance: one entrance may not eat the
        other's day."""
        if resident == SERVER_IDENTITY:
            return self.chat_build_cap
        per = self.start_build.get("per_resident")
        if isinstance(per, dict):
            r = per.get(resident)
            if isinstance(r, dict) and isinstance(r.get("daily_build_cap"), int):
                return r["daily_build_cap"]
        cap = self.start_build.get("daily_build_cap", DEFAULT_DAILY_BUILD_CAP)
        return cap if isinstance(cap, int) else DEFAULT_DAILY_BUILD_CAP

    def _count_builds_today(self, resident: str, today: str) -> int:
        """Builds this principal GENUINELY STARTED today (UTC) — a seat's spec
        builds, or the server's chat builds."""
        n = 0
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    if "build" not in raw or resident not in raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (rec.get("resident") == resident
                            and rec.get("verb") in ("start-build", BUILD_VERB)
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
                if resident == SERVER_IDENTITY:
                    raise VerbError(
                        BUILD_REFUSED,
                        f"today's chat build budget ({cap}) is spent")
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

    def _caller_identity(self, uid: int, pid: Optional[int]) -> Optional[str]:
        """The principal behind one connection.

        [uids] answers by peer uid; the server is the one caller that shares a
        uid with a human, so its cgroup has to name [server].unit as well."""
        name = self.uid_map.get(uid)
        if name != SERVER_PEER_IDENTITY or pid is None:
            return name
        unit = self.server_unit
        want = unit if "/" in unit else f"/system.slice/{unit}"
        if any(line.rstrip().rpartition(":")[2] == want
               for line in self._read_peer_cgroup(pid).splitlines()):
            return SERVER_IDENTITY
        return name

    def dispatch(self, uid: int, verb: Any, args: Any, *,
                 pid: Optional[int] = None) -> dict:
        """Authorize + execute one request."""
        resident = self._caller_identity(uid, pid)
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
            # A third slot is audit extras: the markers a daily count reads back.
            result, summary, extra = out if len(out) == 3 else (*out, None)
        except VerbError as exc:
            allowed = exc.code not in ("bad-args", "over-budget", "verb-disabled",
                                       "apps-refused", BUILD_REFUSED,
                                       MERGE_REFUSED)
            if reserved and not allowed:
                self._release_action(resident)
            reason = f" ({exc.reason})" if exc.reason else ""
            self._audit(caller, verb, args, allowed,
                        f"{'error' if allowed else 'denied'}: {exc.message}{reason}")
            return self._err(exc.code, exc.message, reason=exc.reason)
        except Exception as exc:  # noqa: BLE001 — never crash the daemon on a verb
            self._audit(caller, verb, args, True, f"error: internal: {exc!r}")
            return self._err("internal", "internal broker error")

        self._audit(caller, verb, args, True, summary, extra=extra)
        return {"ok": True, "verb": verb, "result": result}

    @staticmethod
    def _err(code: str, message: str, reason: Optional[str] = None) -> dict:
        err = {"code": code, "message": message}
        if reason:
            err["reason"] = reason
        return {"ok": False, "error": err}

    # ---------------------------------------------------------- subprocess

    def _argv(self, key: str, default: list[str]) -> list[str]:
        argv = self.commands.get(key, default)
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise VerbError("internal", f"commands.{key} must be a list of strings")
        return list(argv)

    def _run(self, argv: list[str], timeout: int,
             cwd: Optional[str] = None,
             errors: Optional[str] = None) -> subprocess.CompletedProcess:
        # Fixed argv list, shell NEVER involved.
        # `errors` pins the decode for output that carries filenames: a name is
        # arbitrary bytes and must not raise under the daemon's locale.
        try:
            return subprocess.run(  # noqa: S603 — argv list, no shell
                argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                encoding="utf-8" if errors else None, errors=errors,
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
                    slug=slug, branch=branch, origin=meta.get("origin"),
                    publish=self._harvest_report(
                        out_path, self._read_build_tail(out_path)),
                    unit_reason=f"timed out after {timeout}s — killed"
                                + ("" if stopped else
                                   " (unit stop reported a problem; check "
                                   f"systemctl status {build_unit_name(slug)})"))
                return
            except Exception as exc:  # noqa: BLE001 — broken pipe etc. = a failure
                self._narrate_build_outcome(
                    slug=slug, branch=branch, origin=meta.get("origin"),
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
                if meta.get("origin"):
                    self._narrate_build_outcome(
                        slug=slug, branch=branch, publish=publish,
                        origin=meta["origin"],
                        unit_reason=(err_s or session_out).strip()[:400]
                        or "the build seat failed its dependency preflight")
                    return
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
                unit_reason=unit_reason, origin=meta.get("origin"))
        finally:
            self._unlink_build_logs(out_path, err_path)
            self._remove_build_sidecar(slug)
            self._finish_build(slug)

    @staticmethod
    def _tier_of(classification: dict) -> int:
        tier = classification.get("tier")
        if not isinstance(tier, int) or tier not in (0, 1, 2):
            raise Broker._merge_refused(
                "the classifier did not answer with a tier", "tier")
        return tier

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

    def _check_diff_args(self, resident: str, args: dict) -> tuple[str, str]:
        """The repo/range pair every diff verb takes: one mapped host path, and
        a range each side of which is split off and handed to git as a bare
        positional, so no side may parse as a flag."""
        repo = _check_str(args, "repo", required=True, max_len=300)
        assert repo is not None
        if not repo.startswith("/") or "/../" in repo or repo.endswith("/.."):
            raise _bad("repo must be an absolute path without ..")
        rng = _check_str(args, "range", required=True, max_len=200)
        assert rng is not None
        if rng.startswith("-") or not _RANGE_RE.match(rng):
            raise _bad("range must be a plain git rev/range "
                       "(letters, digits, . _ ~ ^ / { } -, no leading dash)")
        for side in rng.replace("...", "..").split(".."):
            if side.startswith("-"):
                raise _bad("neither side of the range may start with '-'")
        return self._map_resident_path(resident, repo, label="repo"), rng

    def _verb_classify_diff(self, resident: str, args: dict) -> tuple[dict, str]:
        """Contract with harness/classifier/classify_diff.py (WP-H4): argv:
        <classify_diff.py> --repo <abs path> --range <git range> --config
        <protected-paths.toml> --gates <json object>; stdout: one JSON object (the
        classification), exit 0."""
        _reject_unknown(args, {"repo", "range", "gates"})
        repo, rng = self._check_diff_args(resident, args)
        gates = args.get("gates", {})
        if not isinstance(gates, dict):
            raise _bad("gates must be an object")
        classification = self._classify(repo, rng, gates)
        tier = classification.get("tier") if isinstance(classification, dict) else None
        return ({"classification": classification}, f"classified: tier={tier}")

    def _verb_changed_files(self, resident: str, args: dict) -> tuple[dict, str]:
        """What a branch DID to the tree. `A..B` and `A...B` both answer for
        `A...B` — a reviewer means the branch's own changes, never main's."""
        _reject_unknown(args, {"repo", "range"})
        repo, rng = self._check_diff_args(resident, args)
        left, right = _split_diff_range(rng)
        timeout = SUBPROCESS_TIMEOUTS["changed-files"]
        git = self._argv("git", ["git"])
        from_sha = self._rev_sha(git, repo, left, "left", timeout)
        to_sha = self._rev_sha(git, repo, right, "right", timeout)
        merge_base = self._run(
            [*git, "-C", repo, "merge-base", from_sha, to_sha], timeout)
        if merge_base.returncode != 0:
            raise _bad(f"the two sides of {rng} share no history")
        spec = f"{from_sha}...{to_sha}"
        numstat = self._git_diff(git, repo, ["--numstat", spec], timeout)
        name_status = self._git_diff(git, repo, ["--name-status", spec], timeout)
        try:
            counts = _parse_numstat(numstat)
            rows = _parse_name_status(name_status)
        except (IndexError, ValueError):
            raise VerbError("exec-failure", "git diff output did not parse") from None

        files = []
        added_total = removed_total = 0
        for status, path, old_path in sorted(rows, key=lambda row: row[1]):
            added, removed = counts.get(path, (None, None))
            added_total += added or 0
            removed_total += removed or 0
            entry = {"path": _safe_path(path), "status": status,
                     "added": added, "removed": removed,
                     "binary": added is None and removed is None}
            if old_path is not None:
                entry["old_path"] = _safe_path(old_path)
            files.append(entry)
        truncated = len(files) > MAX_CHANGED_FILES
        summary = (f"changed-files: {len(files)} files, "
                   f"+{added_total} -{removed_total}"
                   + (" (truncated)" if truncated else ""))
        return ({"base": merge_base.stdout.strip(), "from": from_sha,
                 "to": to_sha, "files": files[:MAX_CHANGED_FILES],
                 "totals": {"files": len(files), "added": added_total,
                            "removed": removed_total},
                 "truncated": truncated}, summary)

    def _rev_sha(self, git: list[str], repo: str, rev: str, side: str,
                 timeout: int) -> str:
        """A rev a reviewer named, resolved to a commit sha — or refused by the
        side it came from, because `exec-failure` tells them nothing."""
        cp = self._run([*git, "-C", repo, "rev-parse", "--verify",
                        "--end-of-options", f"{rev}^{{commit}}"], timeout)
        if cp.returncode != 0:
            if "not a git repository" in cp.stderr:
                raise VerbError("exec-failure", "repo is not a git repository")
            raise _bad(f"the {side} side of the range does not resolve: {rev}")
        return cp.stdout.strip()

    def _git_diff(self, git: list[str], repo: str, rest: list[str],
                  timeout: int) -> str:
        """One `-z` diff. The trailing `--` closes the revision list, so nothing
        after it can be read as an option."""
        cp = self._run([*git, "-C", repo, "diff", "-z", "-M", *rest, "--"],
                       timeout, errors="replace")
        if cp.returncode != 0:
            raise VerbError(
                "exec-failure",
                f"git diff exit {cp.returncode}: {cp.stderr.strip()[:300]}")
        return cp.stdout

    def _protected_paths(self) -> str:
        return self.paths.get(
            "protected_paths",
            "/home/plink/Disjorn/Disjorn/harness/classifier/protected-paths.toml")

    def _classify(self, repo: str, rng: str, gates_obj: dict) -> dict:
        """One classifier run — the same argv for a resident's `classify-diff` and
        for the broker's own pre-merge classification."""
        gates_text = json.dumps(gates_obj, ensure_ascii=False)
        if len(gates_text) > MAX_GATES_JSON:
            raise _bad(f"gates JSON exceeds {MAX_GATES_JSON} bytes")
        classifier = self.paths.get(
            "classifier",
            "/home/plink/Disjorn/Disjorn/harness/classifier/classify_diff.py")
        argv = self._argv("classify_diff", [sys.executable, classifier])
        argv += ["--repo", repo, "--range", rng,
                 "--config", self._protected_paths(), "--gates", gates_text]
        cp = self._run(argv, SUBPROCESS_TIMEOUTS["classify-diff"])
        if cp.returncode != 0:
            raise VerbError("exec-failure",
                            f"classifier exit {cp.returncode}: {cp.stderr.strip()[:500]}")
        try:
            out = json.loads(cp.stdout)
        except json.JSONDecodeError:
            raise VerbError("exec-failure", "classifier emitted non-JSON output") from None
        return out if isinstance(out, dict) else {}

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
            peer_pid, uid, _gid = struct.unpack("3i", creds)
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
            resp = self.dispatch(uid, req.get("verb"), req.get("args", {}),
                                 pid=peer_pid)
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
