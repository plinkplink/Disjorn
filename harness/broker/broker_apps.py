"""The apps-build verb and its reaper."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any, Optional

from broker_common import (
    BUILD_ACTIVE_STATES, VerbError, _bad, _check_int, _check_str,
    _reject_unknown,
)


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
    "model": "claude-opus-5-5",
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


class AppsVerbs:

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
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".apps-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({k: v for k, v in rec.items() if k != "started_mono"}, fh)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

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
