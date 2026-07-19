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
  audit log.

Config: /etc/disjorn-broker/broker.toml + verbs.toml (templates alongside this
file). Paths overridable for tests via DISJORN_BROKER_CONFIG /
DISJORN_BROKER_VERBS or --config/--verbs.

Runs as plink (not root) under systemd; the single privileged escape hatch is
the sudoers line in harness/keyboard/90-disjorn-broker.sudoers allowing exactly
`sudo -n systemctl restart disjorn`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
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
}

_RANGE_RE = re.compile(r"^[A-Za-z0-9._~^/{}-]{1,200}$")  # git rev / range; no
# whitespace, no leading dash (checked separately) — can never be read as a flag
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class VerbError(Exception):
    """A verb failed or a request was rejected. code -> PROTOCOL.md error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _bad(msg: str) -> VerbError:
    return VerbError("bad-args", msg)


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
# The broker.
# --------------------------------------------------------------------------

class Broker:
    """Unix-socket verb broker. Construct with parsed broker.toml + a path to
    verbs.toml (re-read per request — that's the kill-switch property)."""

    def __init__(
        self,
        config: dict,
        verbs_path: str,
        *,
        transport: Optional[Callable[[dict, str], dict]] = None,
    ) -> None:
        self.config = config
        self.verbs_path = verbs_path
        self.transport = transport or _sdk_transport
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
        self._audit_lock = threading.Lock()
        self._listener: Optional[socket.socket] = None
        self._closed = False

        # The verb table.  Adding a verb here is a deliberate act; there is no
        # dynamic registration and — enforced by test — no "restart-self".
        self.verbs: dict[str, Callable[[str, dict], tuple[dict, str]]] = {
            "restart-disjorn": self._verb_restart_disjorn,
            "run-server-tests": self._verb_run_server_tests,
            "classify-diff": self._verb_classify_diff,
            "read-prod-logs": self._verb_read_prod_logs,
            "read-own-log": self._verb_read_own_log,
            "read-metrics": self._verb_read_metrics,
            "file-proposal": self._verb_file_proposal,
            "query-own-audit": self._verb_query_own_audit,
        }

    # ------------------------------------------------------------- audit

    def _audit(self, resident: str, verb: str, args: Any, allowed: bool,
               result_summary: str) -> None:
        rec = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "resident": resident,
            "verb": verb,
            "args": args,
            "allowed": allowed,
            "result_summary": result_summary[:500],
        }
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with self._audit_lock:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

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

        try:
            result, summary = self.verbs[verb](resident, args)
        except VerbError as exc:
            allowed = exc.code != "bad-args"  # bad args = a denial, not a run
            self._audit(caller, verb, args, allowed,
                        f"{'error' if allowed else 'denied'}: {exc.message}")
            return self._err(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 — never crash the daemon on a verb
            self._audit(caller, verb, args, True, f"error: internal: {exc!r}")
            return self._err("internal", "internal broker error")

        self._audit(caller, verb, args, True, summary)
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

    def _verb_classify_diff(self, resident: str, args: dict) -> tuple[dict, str]:
        """Contract with harness/classifier/classify_diff.py (WP-H4):
        argv: <classify_diff.py> --repo <abs path> --range <git range>
              --gates-json <json object>; stdout: one JSON object (the
        classification), exit 0. Anything else is exec-failure."""
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
        gates = args.get("gates", {})
        if not isinstance(gates, dict):
            raise _bad("gates must be an object")
        gates_json = json.dumps(gates, ensure_ascii=False)
        if len(gates_json) > MAX_GATES_JSON:
            raise _bad(f"gates JSON exceeds {MAX_GATES_JSON} bytes")
        classifier = self.paths.get(
            "classifier",
            "/home/plink/Disjorn/Disjorn/harness/classifier/classify_diff.py")
        argv = self._argv("classify_diff", [sys.executable, classifier])
        argv += ["--repo", repo, "--range", rng, "--gates-json", gates_json]
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
    broker = Broker(config, ns.verbs)

    def _stop(signum: int, _frame: Any) -> None:
        print(f"disjorn-broker: signal {signum}, shutting down", file=sys.stderr)
        broker.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"disjorn-broker: listening on {broker.socket_path} "
          f"(config={ns.config}, verbs={ns.verbs})", file=sys.stderr)
    broker.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
