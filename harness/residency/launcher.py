"""Container-launch contract for a headless Claude Code session (WP-H9).

On summon the adapter spawns a headless CC session in Gable's container by
invoking a configured command — in production the run-resident.sh wrapper
(harness/cc/run-resident.sh), in tests a stub script. This module owns that
contract and NOTHING else decides the argv.

THE argv IS CONFIG, NEVER CHAT. The command executed is exactly::

    [*container.command, container.resident, *container.session_argv]
    (+ ["--model", container.model] when a model is pinned — WP-L5)

every element sourced from the plink-owned config. The assembled session
prompt — which DOES contain channel text — is handed to the process on STDIN,
never spliced into argv. This is the load-bearing "chat is data, never
authorization" boundary at the process layer: chat can shape what CC reads,
never what the adapter executes. The model pin extends the argv contract but
never the chat boundary: the pin comes from config like everything else.

The launched command is expected to emit the session result on stdout. The
parser is tolerant of both this adapter's own envelope and Claude Code's
``--output-format json`` shape:

* reply text  <- first present of: reply, result, text, content
* action count<- first present of: action_count, num_turns, actions, turns
* model id    <- explicit ``model``, else the (single) key of Claude Code's
                 per-model breakdown ``modelUsage`` (WP-L5 assert signal;
                 see parse_model). Best-effort — may be None if the envelope
                 carries no model info at all.

Non-JSON stdout is taken verbatim as the reply (action count / model unknown).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from config import ContainerConfig

__all__ = ["SessionResult", "ContainerLauncher"]

_REPLY_KEYS = ("reply", "result", "text", "content")
_ACTION_KEYS = ("action_count", "num_turns", "actions", "turns")


@dataclass
class SessionResult:
    ok: bool
    reply: str = ""
    action_count: Optional[int] = None
    # The model the session actually reported using, when the output envelope
    # carries it (WP-L5). None = not knowable from this output — the adapter
    # then asserts at the strongest level available (the pin) and says so.
    model: Optional[str] = None
    duration_sec: float = 0.0
    exit_code: Optional[int] = None
    error: Optional[str] = None


def _first_int(data: dict) -> Optional[int]:
    for key in _ACTION_KEYS:
        v = data.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def _first_str(data: dict) -> Optional[str]:
    for key in _REPLY_KEYS:
        v = data.get(key)
        if isinstance(v, str):
            return v
    return None


def parse_model(data: dict) -> Optional[str]:
    """The model id a Claude Code result envelope reports having used (WP-L5).

    Claude Code's ``--output-format json`` result does not guarantee a
    top-level ``model`` field, but it does carry a per-model cost/usage
    breakdown (``modelUsage``) keyed by full model id. Preference order:

    * explicit ``model`` string, if present;
    * the sole key of ``modelUsage`` — the summon session runs one pinned
      model, so a single key is the normal, unambiguous case. If several
      models appear (e.g. a subagent used another), the first key is taken as
      the primary and the adapter's membership check still catches drift.

    Returns None when the envelope carries no model info — a real gap the
    adapter surfaces rather than papering over.
    """
    m = data.get("model")
    if isinstance(m, str) and m.strip():
        return m.strip()
    usage = data.get("modelUsage")
    if isinstance(usage, dict):
        keys = [k for k in usage if isinstance(k, str) and k.strip()]
        if keys:
            return keys[0].strip()
    return None


def parse_output(stdout: str) -> tuple[str, Optional[int], Optional[str]]:
    """(reply, action_count, model) from a launched session's stdout."""
    text = stdout.strip()
    if not text:
        return "", None, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text, None, None
    if isinstance(data, dict):
        reply = _first_str(data)
        return (reply if reply is not None else text), _first_int(data), parse_model(data)
    return text, None, None


class ContainerLauncher:
    def __init__(self, config: "ContainerConfig") -> None:
        self.config = config

    def build_argv(self) -> list[str]:
        """The exact argv — a pure function of config, independent of any prompt.

        When a model is pinned (WP-L5), ``--model <id>`` is appended so the
        session runs the pinned model, never the account default. It rides at
        the tail so a bash-wrapped session_argv (prod: ``bash -lc '... exec
        claude ... "$@"' <argv0>``) forwards it straight to claude — see
        summon.toml.template. No fallback: an unrunnable pin fails loud.
        """
        c = self.config
        if not c.command:
            raise ValueError("container.command is empty; nothing to launch")
        argv = [*c.command, c.resident, *c.session_argv]
        if c.model:
            argv += ["--model", c.model]
        return argv

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.config.env)  # config-supplied extras only
        return env

    async def run(self, prompt: str) -> SessionResult:
        """Launch one session, feed ``prompt`` on stdin, return the result.

        Never raises for a subprocess failure/timeout — those come back as a
        SessionResult with ``ok=False`` so the daemon degrades gracefully.
        """
        argv = self.build_argv()
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
        except OSError as exc:
            return SessionResult(
                ok=False,
                duration_sec=time.monotonic() - started,
                error=f"launch failed: {exc}",
            )

        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=self.config.timeout_sec,
            )
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001 — already reaping
                pass
            return SessionResult(
                ok=False,
                duration_sec=time.monotonic() - started,
                error=f"session timed out after {self.config.timeout_sec}s",
            )

        duration = time.monotonic() - started
        exit_code = proc.returncode
        stdout = (out or b"").decode("utf-8", "replace")
        stderr = (err or b"").decode("utf-8", "replace")
        if exit_code != 0:
            return SessionResult(
                ok=False,
                exit_code=exit_code,
                duration_sec=duration,
                error=f"session exit {exit_code}: {stderr.strip()[:500]}",
            )

        reply, actions, model = parse_output(stdout)
        return SessionResult(
            ok=True,
            reply=reply,
            action_count=actions,
            model=model,
            exit_code=exit_code,
            duration_sec=duration,
        )
