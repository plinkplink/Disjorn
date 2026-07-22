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

Output parsing
--------------

The launched command emits the session result on stdout. Two shapes are
supported, auto-detected — the launcher never needs to be told which one the
configured ``session_argv`` produces:

* **stream-json** (``claude -p --output-format stream-json --verbose``): one
  JSON object per line, in real time. The first line is a ``system``/``init``
  event that names the **resolved model before the turn runs**; later
  ``assistant`` events carry the model of each main-loop turn; a final
  ``result`` event carries ``result`` / ``num_turns`` / ``modelUsage``. This is
  the shape that makes the BL-G1 pre-act model gate possible (StreamGate).
* **single-object json** (``claude -p --output-format json``) or any other
  stdout: parsed after the fact by ``parse_output`` — the legacy path,
  behaviour unchanged.

Either way the assembled SessionResult means the same thing:

* reply text  <- first present of: reply, result, text, content
* action count<- first present of: action_count, num_turns, actions, turns
* model id    <- the init event's ``model`` (authoritative, pre-act) when
                 streaming, else the last main-loop ``assistant`` model, else
                 the legacy best-effort ``parse_model`` of the result envelope.

Non-JSON stdout is taken verbatim as the reply (action count / model unknown).

BL-G1 pre-act model gate
------------------------

WP-L5's model assert is post-hoc: with ``--output-format json`` the actual
model id is only knowable from the FINISHED session's envelope, so a mismatch
is alerted *after* the reply has already shipped. The init event closes that
gap — StreamGate reads the resolved model off it and can kill the session
before a single token of reply exists. The gate is config-gated by
``container.model_gate`` and ships **off**; see config._parse_model_gate.

Fail loud, never fail over: the gate never retries, never substitutes a model,
never downgrades a refusal to a warning. When it is off it does not gate — it
does not half-gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from config import MODEL_GATE_ALERT, MODEL_GATE_OFF, MODEL_GATE_REFUSE

if TYPE_CHECKING:  # pragma: no cover
    from config import ContainerConfig

logger = logging.getLogger("disjorn.residency.launcher")

__all__ = [
    "SessionResult",
    "ContainerLauncher",
    "StreamGate",
    "GateVerdict",
    "parse_model",
    "parse_output",
    "parse_event_model",
]

_REPLY_KEYS = ("reply", "result", "text", "content")
_ACTION_KEYS = ("action_count", "num_turns", "actions", "turns")

# Gate stages — which observation refused the session. Carried on SessionResult
# so the adapter's alert can say precisely what happened.
STAGE_INIT = "init"                  # init event named the wrong model
STAGE_INIT_NO_MODEL = "init-no-model"  # init event arrived without a model id
STAGE_MID_SESSION = "mid-session"     # a main-loop turn changed model after init
STAGE_NO_INIT = "no-init"             # session ended without ever naming a model


@dataclass
class GateVerdict:
    """A refusal by the pre-act model gate: what was expected, what was seen."""

    stage: str
    expected: Optional[str]
    actual: Optional[str]

    def message(self) -> str:
        if self.stage == STAGE_INIT:
            return (
                f"model gate: session started on {self.actual} but the pin is "
                f"{self.expected} — refused before the turn ran"
            )
        if self.stage == STAGE_MID_SESSION:
            return (
                f"model gate: session started on {self.expected} then switched "
                f"to {self.actual} mid-session — killed, nothing posted"
            )
        if self.stage == STAGE_INIT_NO_MODEL:
            return (
                f"model gate: session init event carried no model id, so the "
                f"pin {self.expected} could not be verified before the turn ran"
            )
        return (
            f"model gate: session produced no system/init event, so the pin "
            f"{self.expected} could not be verified before the turn ran — "
            f"container.model_gate=\"refuse\" requires a session_argv using "
            f"`--output-format stream-json --verbose`"
        )


@dataclass
class SessionResult:
    ok: bool
    reply: str = ""
    action_count: Optional[int] = None
    # The model the session actually reported using (WP-L5). With a stream this
    # is the init event's resolved id — a fact, known before the turn ran. With
    # the legacy json envelope it is best-effort and may be None: not knowable
    # from this output. The adapter then asserts at the strongest level
    # available (the pin) and says so.
    model: Optional[str] = None
    duration_sec: float = 0.0
    exit_code: Optional[int] = None
    error: Optional[str] = None
    # BL-G1: set when the pre-act model gate refused this session. The adapter
    # posts NOTHING the session produced when this is true.
    gate_abort: bool = False
    gate_stage: Optional[str] = None
    gate_expected: Optional[str] = None
    gate_actual: Optional[str] = None
    # Distinct main-loop model ids observed across the session, in order of
    # first appearance. Length > 1 means the model changed mid-session (the
    # sticky-safeguard-switch shape). Empty for the legacy json path.
    models_seen: list[str] = field(default_factory=list)


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


def _clean_model(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_model(data: dict) -> Optional[str]:
    """The model id a Claude Code *result* envelope reports (WP-L5, legacy).

    Claude Code's ``--output-format json`` result carries no top-level ``model``
    field, only a per-model cost/usage breakdown (``modelUsage``) keyed by full
    model id. Preference order:

    * explicit ``model`` string, if present;
    * the first key of ``modelUsage``.

    KNOWN WEAKNESS, and the reason the stream path does not use this: a real
    session's ``modelUsage`` routinely holds MORE than the pinned model — an
    observed local probe of ``claude -p --model claude-fable-5`` came back with
    ``modelUsage`` keys ``["claude-haiku-4-5-20251001", "claude-fable-5"]``,
    because CC uses a small model for background work. "First key" is therefore
    not reliably "the model that answered": it can name an auxiliary model (a
    false drift) and, when a session genuinely changes model part-way, it cannot
    say which one wrote the reply. The stream's init/assistant events state the
    answering model outright, so ``StreamGate`` prefers them and this function
    is only the fallback for non-streaming output.

    Returns None when the envelope carries no model info — a real gap the
    adapter surfaces rather than papering over.
    """
    m = _clean_model(data.get("model"))
    if m:
        return m
    usage = data.get("modelUsage")
    if isinstance(usage, dict):
        keys = [k for k in usage if isinstance(k, str) and k.strip()]
        if keys:
            return keys[0].strip()
    return None


def parse_event_model(event: dict) -> Optional[str]:
    """The model id a stream-json event names, if it names one.

    Two event shapes state a model authoritatively:

    * ``{"type": "system", "subtype": "init", "model": "<id>"}`` — the resolved
      model, emitted BEFORE the turn produces anything;
    * ``{"type": "assistant", "message": {"model": "<id>"}, ...}`` — the model
      of that turn.

    Assistant events carrying a ``parent_tool_use_id`` come from a subagent,
    which may legitimately run a different model than the main loop; those are
    NOT the session's model and return None here, so the gate never refuses a
    session over a subagent's model choice.
    """
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        return _clean_model(event.get("model"))
    if etype == "assistant":
        if event.get("parent_tool_use_id"):
            return None
        msg = event.get("message")
        if isinstance(msg, dict):
            return _clean_model(msg.get("model"))
    return None


def parse_output(stdout: str) -> tuple[str, Optional[int], Optional[str]]:
    """(reply, action_count, model) from a launched session's stdout.

    The legacy single-envelope path (``--output-format json`` or anything else
    that is not a JSON stream). Unchanged from WP-L5.
    """
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


class StreamGate:
    """Incremental consumer of ``--output-format stream-json`` + the BL-G1 gate.

    Fed one stdout line at a time as the session runs. It does two jobs:

    1. **Gate** (only when ``mode`` is alert/refuse AND a pin is set): compare
       the model named by each authoritative event against the pin. In
       ``refuse`` mode a mismatch returns a GateVerdict, and the caller kills
       the session on the spot — at the init event that is genuinely pre-act,
       before any reply text exists. In ``alert`` mode the same mismatch is
       logged loudly at the moment it is seen and the session runs on.
    2. **Assemble** the same SessionResult fields the legacy json parse
       produced — ``result`` -> reply, ``num_turns`` -> action_count — plus a
       model id that is a fact rather than an inference.

    A line that is not a JSON object, or a JSON object with no ``type``, is
    ignored for gating and assembly but still counted as noise: if NOTHING in
    the stream parsed as an event, ``saw_events`` stays False and the caller
    falls back to ``parse_output`` over the whole stdout. That fallback is what
    keeps a stream-parse surprise from bricking summons while the gate is off.
    """

    def __init__(self, pin: Optional[str], mode: str = MODEL_GATE_OFF) -> None:
        self.pin = pin
        self.mode = mode if mode in (MODEL_GATE_ALERT, MODEL_GATE_REFUSE) else MODEL_GATE_OFF
        self.saw_events = False
        self.init_model: Optional[str] = None
        self.saw_init = False
        self.models_seen: list[str] = []
        self.result_event: Optional[dict] = None
        # Recorded for the caller/logs in alert mode (refuse mode returns a
        # verdict instead of recording).
        self.mismatch: Optional[GateVerdict] = None

    # ------------------------------------------------------------- properties

    @property
    def active(self) -> bool:
        """True when the gate has something to enforce: a mode and a pin."""
        return self.mode != MODEL_GATE_OFF and bool(self.pin)

    @property
    def model(self) -> Optional[str]:
        """The session's model: init (authoritative) > last main-loop turn >
        legacy result-envelope inference."""
        if self.init_model:
            return self.init_model
        if self.models_seen:
            return self.models_seen[-1]
        if self.result_event is not None:
            return parse_model(self.result_event)
        return None

    @property
    def reply(self) -> Optional[str]:
        if self.result_event is None:
            return None
        return _first_str(self.result_event)

    @property
    def action_count(self) -> Optional[int]:
        if self.result_event is None:
            return None
        return _first_int(self.result_event)

    @property
    def switched_mid_session(self) -> bool:
        """The model changed between main-loop turns (sticky-switch shape)."""
        return len(self.models_seen) > 1

    # ----------------------------------------------------------------- intake

    def feed_line(self, line: str) -> Optional[GateVerdict]:
        """Consume one stdout line. Returns a verdict iff the session must die."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return None
        self.saw_events = True

        etype = event["type"]
        if etype == "result":
            self.result_event = event
            return None

        model = parse_event_model(event)
        is_init = etype == "system" and event.get("subtype") == "init"
        if is_init:
            self.saw_init = True
            self.init_model = model
            return self._check_init(model)
        if model:
            if model not in self.models_seen:
                self.models_seen.append(model)
            return self._check_turn(model)
        return None

    def finish(self) -> Optional[GateVerdict]:
        """Called once at end of stream. Returns a verdict iff the session must
        be treated as refused.

        The documented rule for a missing init event: in ``refuse`` mode, no
        init event means the gate never got to check anything, which is a
        failure to verify, not a pass. Refuse. (Almost always this means the
        deployment's ``session_argv`` still emits ``--output-format json`` —
        the verdict message says so.) In ``off``/``alert`` mode nothing is
        refused; ``alert`` logs that the pin went unverified pre-act, and the
        adapter's existing post-hoc "unverified" suffix still marks the reply.
        """
        if not self.active:
            return None
        if self.saw_init and self.init_model:
            return None
        if self.mode == MODEL_GATE_REFUSE:
            stage = STAGE_INIT_NO_MODEL if self.saw_init else STAGE_NO_INIT
            verdict = GateVerdict(stage=stage, expected=self.pin, actual=None)
            logger.error("%s", verdict.message())
            return verdict
        logger.warning(
            "model gate (alert): session never named a model before the turn "
            "ran; pin %s unverified pre-act", self.pin,
        )
        return None

    # ----------------------------------------------------------------- checks

    def _check_init(self, model: Optional[str]) -> Optional[GateVerdict]:
        if not self.active:
            return None
        if model is None:
            # Malformed init: it arrived, but without a model id. finish()
            # applies the documented rule; nothing to compare here.
            return None
        if model == self.pin:
            return None
        verdict = GateVerdict(stage=STAGE_INIT, expected=self.pin, actual=model)
        logger.error("%s", verdict.message())
        if self.mode == MODEL_GATE_REFUSE:
            return verdict
        self.mismatch = verdict
        return None

    def _check_turn(self, model: str) -> Optional[GateVerdict]:
        if not self.active or model == self.pin:
            return None
        # Only interesting as a *change*: if init already mismatched, the init
        # verdict/alert above is the story and this would be a duplicate.
        if self.init_model and self.init_model != self.pin:
            return None
        verdict = GateVerdict(
            stage=STAGE_MID_SESSION, expected=self.pin, actual=model
        )
        logger.error("%s", verdict.message())
        if self.mode == MODEL_GATE_REFUSE:
            return verdict
        self.mismatch = self.mismatch or verdict
        return None


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

    @staticmethod
    async def _kill(proc) -> None:
        try:
            proc.kill()
        except ProcessLookupError:  # already gone
            return
        except Exception:  # noqa: BLE001 — best effort reaping
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001 — already reaping
            pass

    async def run(self, prompt: str) -> SessionResult:
        """Launch one session, feed ``prompt`` on stdin, return the result.

        Never raises for a subprocess failure/timeout — those come back as a
        SessionResult with ``ok=False`` so the daemon degrades gracefully.

        stdout is consumed incrementally so the BL-G1 gate can act on the
        session's ``system``/``init`` event while the turn is still ahead of it.
        """
        argv = self.build_argv()
        gate = StreamGate(self.config.model, getattr(self.config, "model_gate", MODEL_GATE_OFF))
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

        stdout, stderr, verdict, timed_out = await self._pump(proc, prompt, gate, started)
        duration = time.monotonic() - started

        if verdict is not None:
            logger.error(
                "session refused by model gate (%s): expected %s, saw %s",
                verdict.stage, verdict.expected, verdict.actual,
            )
            return SessionResult(
                ok=False,
                duration_sec=duration,
                exit_code=proc.returncode,
                error=verdict.message(),
                gate_abort=True,
                gate_stage=verdict.stage,
                gate_expected=verdict.expected,
                gate_actual=verdict.actual,
                model=verdict.actual,
                models_seen=list(gate.models_seen),
            )

        if timed_out:
            return SessionResult(
                ok=False,
                duration_sec=duration,
                error=f"session timed out after {self.config.timeout_sec}s",
                models_seen=list(gate.models_seen),
            )

        exit_code = proc.returncode
        if exit_code != 0:
            return SessionResult(
                ok=False,
                exit_code=exit_code,
                duration_sec=duration,
                error=f"session exit {exit_code}: {stderr.strip()[:500]}",
                models_seen=list(gate.models_seen),
            )

        # End-of-stream rule (missing/model-less init). Only reachable when the
        # gate is enforcing; off/alert return None.
        verdict = gate.finish()
        if verdict is not None:
            return SessionResult(
                ok=False,
                duration_sec=duration,
                exit_code=exit_code,
                error=verdict.message(),
                gate_abort=True,
                gate_stage=verdict.stage,
                gate_expected=verdict.expected,
                gate_actual=verdict.actual,
                models_seen=list(gate.models_seen),
            )

        if gate.saw_events:
            reply = gate.reply
            actions = gate.action_count
            model = gate.model
            if reply is None:
                # A stream that parsed as events but never produced a result
                # event: take the raw stdout verbatim rather than invent a
                # reply, exactly as the legacy path does for odd output.
                reply = stdout.strip()
        else:
            reply, actions, model = parse_output(stdout)

        if gate.switched_mid_session:
            logger.warning(
                "model changed mid-session: %s (pin %s)",
                " -> ".join(gate.models_seen), self.config.model or "unpinned",
            )

        return SessionResult(
            ok=True,
            reply=reply,
            action_count=actions,
            model=model,
            exit_code=exit_code,
            duration_sec=duration,
            models_seen=list(gate.models_seen),
        )

    # ------------------------------------------------------------------ pump

    async def _pump(
        self, proc, prompt: str, gate: StreamGate, started: float
    ) -> tuple[str, str, Optional[GateVerdict], bool]:
        """Feed stdin, drain stderr, and read stdout line-wise until the gate
        refuses, the deadline passes, or the stream ends.

        Returns (stdout_text, stderr_text, verdict, timed_out). Reads in raw
        chunks rather than ``StreamReader.readline`` because a stream-json line
        (a whole assistant message) can exceed asyncio's 64 KiB line limit,
        which would raise mid-session and lose output.
        """
        deadline = started + self.config.timeout_sec

        async def feed() -> None:
            if proc.stdin is None:
                return
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # session closed stdin early; not our failure to report
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass

        async def drain_err() -> bytes:
            if proc.stderr is None:
                return b""
            try:
                return await proc.stderr.read()
            except Exception:  # noqa: BLE001
                return b""

        feeder = asyncio.ensure_future(feed())
        errtask = asyncio.ensure_future(drain_err())

        chunks: list[bytes] = []
        buf = bytearray()
        verdict: Optional[GateVerdict] = None
        timed_out = False

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(65536), timeout=remaining
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    timed_out = True
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    verdict = gate.feed_line(line.decode("utf-8", "replace"))
                    if verdict is not None:
                        break
                if verdict is not None:
                    break
            if verdict is None and not timed_out and buf:
                # Trailing line with no newline (the single-object json shape).
                verdict = gate.feed_line(bytes(buf).decode("utf-8", "replace"))
        finally:
            if not feeder.done():
                feeder.cancel()
            try:
                await feeder
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        if verdict is not None or timed_out:
            # Kill FIRST, then drain: stderr is read to EOF, so waiting on it
            # before the kill would let the very session we just refused run to
            # completion — the abort has to bite here or it isn't pre-act.
            await self._kill(proc)
        else:
            try:
                await asyncio.wait_for(proc.wait(), timeout=max(1.0, deadline - time.monotonic()))
            except (asyncio.TimeoutError, TimeoutError):
                timed_out = True
                await self._kill(proc)

        try:
            stderr = await asyncio.wait_for(errtask, timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            errtask.cancel()
            stderr = b""
        except Exception:  # noqa: BLE001
            stderr = b""

        return (
            b"".join(chunks).decode("utf-8", "replace"),
            (stderr or b"").decode("utf-8", "replace"),
            verdict,
            timed_out,
        )
