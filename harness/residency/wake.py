"""The wake lane: one headless work session per human wake (2026-08-25).

SPECS/2026-08-25-agentic-residents.md. plink wakes a seat with a task from his
own uid at the broker socket; the broker authenticates him, writes one record
into a plink-owned spool, and returns. Everything after that is this module,
running as res-<seat> on the host:

    WakeSpool.due()  ──▶  WakeRunner.serve(record)
                              │  accounting: wake-start line
                              │  gatehouse: loop/* refs BEFORE
                              ▼
                   ContainerLauncher.run(prompt)   # cap from the RECORD
                              │
                              │  gatehouse: loop/* refs AFTER
                              ▼
                   #custodian result post  +  accounting: wake-end line

Three rules shape all of it.

**The wrapper posts, not the session.** Every field of the result post is
something this process observed from outside the session — the exit status, the
wall clock it held, the lines the container appended to the house action log,
the gatehouse refs before and after. A banner is evidence only when the process
that posts it is not the process it describes; that is the rule that fixed the
build-done banner, and a woken session is a build by another name.

**Silence is the defect.** The premise of the lane is that a human is waiting,
so every terminal state ends in a post: finished, killed at the cap, crashed,
refused by the model gate, or missed entirely because this daemon was down when
the wake was asked for. There is no path that ends quietly.

**The caps are not this daemon's to set.** The wall clock rides on the wake
record, sourced from plink-owned broker config. A widening is a witnessed edit
to broker.toml, never a value in this seat's own config file. The per-session
ACTION cap is not here at all: a woken session runs in the same container, on
the same /config/budget.json, as a summon.

Nothing in this module can create a wake. The spool is read-only to this uid by
placement (the broker refuses to start unless it is), which is what makes
"nothing self-wakes" a property rather than a promise.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

from config import AdapterConfig
from launcher import ContainerLauncher
from prompt import assemble_wake_prompt
from summary import (
    format_drift_alert,
    format_gate_refusal_alert,
    format_wake_done,
    format_wake_failed,
    format_wake_missed,
)

logger = logging.getLogger("disjorn.residency.wake")

__all__ = [
    "WakeRecord",
    "WakeSpool",
    "GatehouseWatch",
    "WakeAccounting",
    "WakeRunner",
    "SPOOL_SUFFIX",
]

SPOOL_SUFFIX = ".wake.json"
# Kept in the served-state file. Bounded so a long-lived seat's state file does
# not grow without limit; far longer than any record's window, so a wake can
# never be served twice while its record is still on disk.
MAX_SERVED_IDS = 500
GIT_TIMEOUT_SEC = 20
# Appended to the container name for a woken session (run-resident.sh reads
# RESIDENT_CONTAINER_SUFFIX). The summon lane leaves it unset and keeps today's
# name; see WakeRunner._default_launcher for why the two must differ.
WAKE_CONTAINER_SUFFIX = "wake"
# Loop branches are the writable surface a wake may reach (through the
# gatehouse). Anything else in those repos is not this lane's business to watch.
LOOP_PREFIX = "refs/heads/loop/"


@dataclass
class WakeRecord:
    """One wake, as the broker wrote it. Read-only to this process."""

    wake_id: str
    resident: str
    woken_by: str
    requested_at: str
    task: str
    session_cap_sec: int
    grace_sec: int
    path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict, *, path: Optional[str] = None) -> "WakeRecord":
        """Build a record, or raise ValueError. Every field is required: a
        record missing one is a record this runner cannot honour, and guessing
        a default for the CAP would be guessing how long to let a session run."""
        try:
            wake_id = str(data["wake_id"])
            resident = str(data["resident"])
            woken_by = str(data["woken_by"])
            requested_at = str(data["requested_at"])
            task = str(data["task"])
            cap = int(data["session_cap_sec"])
            grace = int(data["grace_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed wake record: {exc}") from None
        if not task.strip():
            raise ValueError("wake record carries no task")
        if cap <= 0:
            raise ValueError(f"wake record cap is not positive: {cap}")
        return cls(wake_id=wake_id, resident=resident, woken_by=woken_by,
                   requested_at=requested_at, task=task, session_cap_sec=cap,
                   grace_sec=max(0, grace), path=path)

    def requested_epoch(self) -> Optional[float]:
        try:
            when = _dt.datetime.fromisoformat(self.requested_at)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return when.timestamp()

    def expired(self, now: float) -> bool:
        """Past the cap plus the grace margin — the window in which a human is
        still waiting for this wake. A record with an unreadable timestamp is
        treated as expired: unrunnable is safer than perpetually runnable."""
        started = self.requested_epoch()
        if started is None:
            return True
        return now > started + self.session_cap_sec + self.grace_sec


class WakeSpool:
    """The plink-owned spool, read through this seat's own served-id state.

    Two lists come out of it: wakes that are DUE (inside their window and never
    served) and wakes that were MISSED (window gone by, never served). Both are
    terminal for this runner — the missed ones get a post rather than a session.
    """

    def __init__(self, spool_dir: str, state_path: str) -> None:
        self.spool_dir = spool_dir
        self.state_path = state_path
        self._served: list[str] = self._load()

    # ------------------------------------------------------------ served ids

    def _load(self) -> list[str]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
        served = data.get("served") if isinstance(data, dict) else None
        return [str(s) for s in served] if isinstance(served, list) else []

    def _save(self) -> None:
        tmp = f"{self.state_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"served": self._served[-MAX_SERVED_IDS:]}, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            logger.warning("failed to persist served wake ids to %s",
                           self.state_path, exc_info=True)

    def served(self, wake_id: str) -> bool:
        return wake_id in self._served

    def mark_served(self, wake_id: str) -> None:
        """Record a wake as handled BEFORE its session runs.

        Before, not after, and deliberately: a crash mid-session must not leave
        a wake that the next poll starts over. A wake is one session; a wake
        that died is an incident for a human, not a retry."""
        if wake_id not in self._served:
            self._served.append(wake_id)
            self._save()

    # --------------------------------------------------------------- reading

    def _records(self) -> list[WakeRecord]:
        try:
            names = sorted(os.listdir(self.spool_dir))
        except OSError:
            logger.warning("wake spool unreadable: %s", self.spool_dir,
                           exc_info=True)
            return []
        out: list[WakeRecord] = []
        for name in names:
            if not name.endswith(SPOOL_SUFFIX):
                continue
            path = os.path.join(self.spool_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                logger.warning("skipping unreadable wake record %s", path,
                               exc_info=True)
                continue
            try:
                out.append(WakeRecord.from_dict(data, path=path))
            except ValueError as exc:
                logger.error("skipping wake record %s: %s", path, exc)
        return out

    def poll(self, resident: str, now: float) -> tuple[list[WakeRecord],
                                                       list[WakeRecord]]:
        """(due, missed) for this seat, oldest first."""
        due: list[WakeRecord] = []
        missed: list[WakeRecord] = []
        for rec in self._records():
            if rec.resident != resident or self.served(rec.wake_id):
                continue
            (missed if rec.expired(now) else due).append(rec)
        key = lambda r: r.requested_at  # noqa: E731 — one-line sort key
        return sorted(due, key=key), sorted(missed, key=key)


class GatehouseWatch:
    """loop/* branch heads in the bare gatehouse repos, before and after.

    This is how a failure post can name a branch and quote its head subject
    without asking the session anything. The session pushes into the gatehouse
    (the one writable path out of the container); this reads the same refs from
    the host, which is why a killed session's work is still legible.

    Unconfigured means unobserved, and the post says so — "not observed" and
    "no branch" must not read alike.
    """

    def __init__(self, gatehouse_dir: Optional[str],
                 runner=subprocess.run) -> None:
        self.gatehouse_dir = gatehouse_dir
        self._run = runner

    @property
    def configured(self) -> bool:
        return bool(self.gatehouse_dir)

    def _git(self, repo: str, *args: str) -> Optional[str]:
        try:
            cp = self._run(["git", "-C", repo, *args], capture_output=True,
                           text=True, timeout=GIT_TIMEOUT_SEC)
        except (OSError, subprocess.SubprocessError):
            logger.warning("git %s failed in %s", " ".join(args), repo,
                           exc_info=True)
            return None
        if cp.returncode != 0:
            logger.warning("git %s in %s: %s", " ".join(args), repo,
                           (cp.stderr or "").strip()[:200])
            return None
        return cp.stdout

    def _repos(self) -> list[str]:
        if not self.gatehouse_dir:
            return []
        try:
            names = sorted(os.listdir(self.gatehouse_dir))
        except OSError:
            logger.warning("gatehouse unreadable: %s", self.gatehouse_dir,
                           exc_info=True)
            return []
        return [os.path.join(self.gatehouse_dir, n)
                for n in names if n.endswith(".git")]

    def snapshot(self) -> Optional[dict[tuple[str, str], str]]:
        """{(repo, ref): sha} over every gatehouse repo's loop branches, or
        None when there is no gatehouse to look at."""
        if not self.configured:
            return None
        out: dict[tuple[str, str], str] = {}
        for repo in self._repos():
            name = os.path.basename(repo)
            listing = self._git(repo, "for-each-ref",
                                "--format=%(refname:short) %(objectname)",
                                LOOP_PREFIX)
            for line in (listing or "").splitlines():
                parts = line.split()
                if len(parts) == 2:
                    out[(name, parts[0])] = parts[1]
        return out

    def _subject(self, repo_name: str, sha: str) -> str:
        if not self.gatehouse_dir:
            return ""
        out = self._git(os.path.join(self.gatehouse_dir, repo_name),
                        "log", "-1", "--format=%s", sha)
        return (out or "").strip()

    def changed(self, before: Optional[dict], after: Optional[dict]) -> Optional[list]:
        """Loop branches this session created or moved, newest state, with the
        head subject and whether it still says `wip:`.

        None (not []) when either snapshot is missing: an unobserved gatehouse
        is not an empty one."""
        if before is None or after is None:
            return None
        out = []
        for (repo, ref), sha in sorted(after.items()):
            if before.get((repo, ref)) == sha:
                continue
            subject = self._subject(repo, sha)
            out.append({
                "repo": repo, "ref": ref, "sha": sha, "subject": subject,
                "created": (repo, ref) not in before,
                # The hand-over signal the wake prompt asks the session for.
                "wip": subject.lower().startswith("wip:"),
            })
        return out


class WakeAccounting:
    """The two lines a wake adds to the house action log (spec, item 4).

    Not a per-seat meter — that stays a design requirement — but enough that a
    runaway is countable from the log instead of anecdotal, and enough that a
    wake with a start and no end is a visible incident.

    The same file the container's PostToolUse counter appends to, so the wake
    lines sit in order among the tool calls they bracket, and so the action
    count in the result post is a DELTA THIS PROCESS MEASURED rather than a
    number the session reported about itself.

    Sharing that file has one honest cost, stated here so nobody rediscovers it
    from a chart: everything counting lines in the action log — the container's
    own daily-cap check (pre-tool-use.py) and the WP-H12 aggregate — counts
    these two as well. Two per wake against a 500/day cap is the price of the
    wake being visible in the same place as everything else the seat did.
    """

    def __init__(self, path: Optional[str]) -> None:
        self.path = path

    @property
    def configured(self) -> bool:
        return bool(self.path)

    def _append(self, record: dict) -> None:
        if not self.path:
            logger.warning("wake accounting is not configured "
                           "([wake].action_log); this wake leaves no house "
                           "action-log trail")
            return
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.error("failed to write wake accounting to %s", self.path,
                         exc_info=True)

    def line_count(self) -> Optional[int]:
        """Lines in the action log right now, for the before/after delta.
        None when there is no log to count — reported as such, never as 0."""
        if not self.path:
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return sum(1 for _ in fh)
        except OSError:
            return None

    @staticmethod
    def _now() -> str:
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def start(self, record: WakeRecord) -> None:
        self._append({"ts": self._now(), "event": "wake-start",
                      "wake_id": record.wake_id, "resident": record.resident,
                      "woken_by": record.woken_by,
                      "cap_sec": record.session_cap_sec, "ok": True})

    def end(self, record: WakeRecord, *, outcome: str, duration_sec: float,
            action_count: Optional[int]) -> None:
        self._append({"ts": self._now(), "event": "wake-end",
                      "wake_id": record.wake_id, "resident": record.resident,
                      "woken_by": record.woken_by, "outcome": outcome,
                      "duration_sec": round(duration_sec, 1),
                      "action_count": action_count,
                      "ok": outcome == "done"})


@dataclass
class WakeOutcome:
    """What the runner observed, and posted. Returned for the log and tests."""

    wake_id: str
    outcome: str                     # done | cap-kill | crash | model-gate | missed
    duration_sec: float = 0.0
    action_count: Optional[int] = None
    branches: Optional[list] = None
    post: str = ""
    extra_posts: list = field(default_factory=list)


class WakeRunner:
    """Serves wakes for one seat: at most one session at a time, ever.

    Sequential by construction rather than by lock: the whole lane is "a human
    asked for one thing and is waiting for it", and two concurrent sessions in
    one seat would share a container name, a home volume and an action log.
    """

    def __init__(
        self,
        client,
        config: AdapterConfig,
        *,
        spool: Optional[WakeSpool] = None,
        watch: Optional[GatehouseWatch] = None,
        accounting: Optional[WakeAccounting] = None,
        launcher_factory=None,
    ) -> None:
        self.client = client
        self.config = config
        wake = config.wake
        if spool is None:
            if not wake.spool_dir:
                raise ValueError(
                    "[wake].spool_dir is not configured; this seat has no wake "
                    "lane (see summon.toml.template)")
            spool = WakeSpool(wake.spool_dir, wake.state_path)
        self.spool = spool
        self.watch = watch or GatehouseWatch(wake.gatehouse_dir)
        self.accounting = accounting or WakeAccounting(wake.action_log)
        self._launcher_factory = launcher_factory or self._default_launcher

    # ------------------------------------------------------------- launching

    def _default_launcher(self, cap_sec: int) -> ContainerLauncher:
        """A launcher on THIS SEAT'S container contract with the wake's cap.

        Same command, same session_argv, same model pin as a summon — the seat
        is the seat, and the agentic seat gets no launch surface a summon seat
        lacks. Two things differ, and neither is chat's or this session's to
        choose: the wall clock, which comes from the record, and the container
        NAME SUFFIX, because run-resident.sh runs with `--replace` and the two
        lanes are independent daemons — without it whichever session starts
        second takes the name and kills the first."""
        return ContainerLauncher(dataclasses.replace(
            self.config.container,
            timeout_sec=float(cap_sec),
            env={**self.config.container.env,
                 "RESIDENT_CONTAINER_SUFFIX": WAKE_CONTAINER_SUFFIX}))

    # ---------------------------------------------------------------- polling

    @property
    def seat(self) -> str:
        """The identity the broker addresses this seat by. `[container].resident`
        is the SHORT name the launch wrapper takes ("gable"); a wake record
        names the uid identity ("res-gable"). One config value, two spellings,
        converted in exactly one place."""
        name = self.config.container.resident
        return name if name.startswith("res-") else f"res-{name}"

    async def poll_once(self, *, now: Optional[float] = None) -> list[WakeOutcome]:
        """Serve every wake due for this seat, and report every missed one."""
        now = now if now is not None else time.time()
        due, missed = await asyncio.to_thread(self.spool.poll, self.seat, now)
        outcomes: list[WakeOutcome] = []
        for record in missed:
            outcomes.append(await self.report_missed(record))
        for record in due:
            outcomes.append(await self.serve(record))
        return outcomes

    # ----------------------------------------------------------------- serving

    async def report_missed(self, record: WakeRecord) -> WakeOutcome:
        self.spool.mark_served(record.wake_id)
        logger.error("wake %s missed: window passed before the runner saw it",
                     record.wake_id)
        post = format_wake_missed(
            wake_id=record.wake_id, resident=record.resident,
            woken_by=record.woken_by, requested_at=record.requested_at)
        await self._safe_send(post)
        return WakeOutcome(wake_id=record.wake_id, outcome="missed", post=post)

    async def serve(self, record: WakeRecord) -> WakeOutcome:
        """Run one woken session and post what happened. Never raises.

        The claim, the accounting start line and the before-snapshot all happen
        BEFORE the launch, so a session that dies in any way — killed at the
        cap, crashed, or taking this daemon down with it — still leaves a start
        with no end for a human to find."""
        self.spool.mark_served(record.wake_id)
        self.accounting.start(record)
        before_actions = await asyncio.to_thread(self.accounting.line_count)
        before_refs = await asyncio.to_thread(self.watch.snapshot)
        logger.info("wake %s: serving %s for %s (cap %ss)", record.wake_id,
                    record.resident, record.woken_by, record.session_cap_sec)

        prompt = assemble_wake_prompt(
            record.task, wake_id=record.wake_id, woken_by=record.woken_by,
            cap_sec=record.session_cap_sec)
        launcher = self._launcher_factory(record.session_cap_sec)

        try:
            result = await launcher.run(prompt)
        except Exception as exc:  # noqa: BLE001 — a wake never ends in silence
            logger.exception("wake %s: launcher raised", record.wake_id)
            return await self._finish(
                record, outcome="crash",
                reason=f"the launcher raised {type(exc).__name__}: {exc}",
                duration_sec=0.0, before_actions=before_actions,
                before_refs=before_refs)

        after_actions = await asyncio.to_thread(self.accounting.line_count)
        actions = self._actions(before_actions, after_actions)
        model, verified, drift = self._model(result)
        if drift:
            # Fail loud, never fail over — the same treatment a summon's drift
            # gets, because the thing that drifted is who was speaking.
            logger.error("model drift on wake %s: pinned %s but session ran %s",
                         record.wake_id, self.config.container.model, model)
            await self._safe_send(format_drift_alert(
                expected=self.config.container.model, actual=model,
                summoner=record.woken_by, where=f"wake {record.wake_id}"))

        if getattr(result, "gate_abort", False):
            extra = format_gate_refusal_alert(
                expected=result.gate_expected, actual=result.gate_actual,
                stage=result.gate_stage or "unknown",
                summoner=record.woken_by, where=f"wake {record.wake_id}")
            return await self._finish(
                record, outcome="model-gate",
                reason="the pre-act model gate refused this session; nothing "
                       "it produced was used",
                duration_sec=result.duration_sec, before_actions=before_actions,
                before_refs=before_refs, actions=actions, model=model,
                model_verified=verified, extra_posts=[extra])

        if getattr(result, "timed_out", False):
            return await self._finish(
                record, outcome="cap-kill",
                reason=f"the wall-clock cap fired at {record.session_cap_sec}s "
                       "— the session was killed, finished or not",
                duration_sec=result.duration_sec, before_actions=before_actions,
                before_refs=before_refs, actions=actions, model=model,
                model_verified=verified)

        if not result.ok:
            return await self._finish(
                record, outcome="crash",
                reason=self._crash_reason(result),
                duration_sec=result.duration_sec, before_actions=before_actions,
                before_refs=before_refs, actions=actions, model=model,
                model_verified=verified)

        return await self._finish(
            record, outcome="done", reason="", duration_sec=result.duration_sec,
            before_actions=before_actions, before_refs=before_refs,
            actions=actions, model=model, model_verified=verified,
            account=self._account(result))

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _crash_reason(result) -> str:
        """What killed a session that was not capped, from its exit status.

        A negative returncode IS a signal (Python's convention), and naming the
        signal is the difference between "it died" and "the OOM killer took
        it"."""
        code = result.exit_code
        if isinstance(code, int) and code < 0:
            head = f"the session died on signal {-code}"
        elif isinstance(code, int):
            head = f"the session exited {code}"
        else:
            head = "the session never ran"
        detail = " ".join((result.error or "").split())[:200]
        return f"{head}: {detail}" if detail else head

    def _model(self, result) -> tuple[Optional[str], bool, bool]:
        """(what to name in the banner, is that a fact, did it drift).

        Same three-way reading the summon path makes (WP-L5): the pin is
        config, the actual is what the session's stream reported, and a pin we
        could not confirm is marked rather than advertised. A gate abort is the
        exception — there the model is the refusal's subject and the session is
        disowned entirely, so the gate's own alert carries it."""
        pin = self.config.container.model
        actual = None if getattr(result, "gate_abort", False) else result.model
        return (actual or pin), actual is not None, bool(
            pin and actual and actual != pin)

    def _account(self, result) -> str:
        reply = " ".join((result.reply or "").split())
        cap = max(0, int(self.config.wake.reply_chars))
        if not reply or not cap:
            return ""
        return reply[:cap] + ("…" if len(reply) > cap else "")

    @staticmethod
    def _actions(before: Optional[int], after: Optional[int]) -> Optional[int]:
        """Tool calls the container logged while the session ran.

        None if either count is missing: an unknown number is reported as
        unknown, never as zero — "0 actions" is a claim about a session that
        did nothing, and this would be a claim about a file we could not read.
        The wake-start line is written before `before` is taken, so it is not
        counted as an action."""
        if before is None or after is None:
            return None
        return max(0, after - before)

    async def _finish(self, record: WakeRecord, *, outcome: str, reason: str,
                      duration_sec: float, before_actions: Optional[int],
                      before_refs: Optional[dict],
                      actions: Optional[int] = None,
                      model: Optional[str] = None,
                      model_verified: bool = False,
                      account: str = "",
                      extra_posts: Optional[list] = None) -> WakeOutcome:
        model = model if model is not None else self.config.container.model
        if actions is None:
            actions = self._actions(
                before_actions, await asyncio.to_thread(self.accounting.line_count))
        after_refs = await asyncio.to_thread(self.watch.snapshot)
        branches = self.watch.changed(before_refs, after_refs)

        if outcome == "done":
            post = format_wake_done(
                wake_id=record.wake_id, resident=record.resident,
                woken_by=record.woken_by, duration_sec=duration_sec,
                cap_sec=record.session_cap_sec, action_count=actions,
                model=model, model_verified=model_verified,
                branches=branches, account=account)
        else:
            post = format_wake_failed(
                wake_id=record.wake_id, resident=record.resident,
                woken_by=record.woken_by, reason=reason,
                duration_sec=duration_sec, cap_sec=record.session_cap_sec,
                action_count=actions, model=model,
                model_verified=model_verified, branches=branches)
            logger.error("wake %s: %s (%s)", record.wake_id, outcome, reason)

        for extra in extra_posts or []:
            await self._safe_send(extra)

        await self._safe_send(post)
        self.accounting.end(record, outcome=outcome, duration_sec=duration_sec,
                            action_count=actions)
        return WakeOutcome(wake_id=record.wake_id, outcome=outcome,
                           duration_sec=duration_sec, action_count=actions,
                           branches=branches, post=post,
                           extra_posts=list(extra_posts or []))

    async def _safe_send(self, content: str) -> None:
        """Post to #custodian. A failed post is logged loudly and never raises:
        the wake is over either way, and the accounting line is the trail that
        survives a server the daemon could not reach."""
        try:
            await self.client.send(
                self.config.summon.custodian_channel_id, content)
        except Exception:  # noqa: BLE001
            logger.error("wake result post FAILED; the post is lost and only "
                         "the action log records this wake: %s", content[:300],
                         exc_info=True)

    # -------------------------------------------------------------- run loop

    async def run(self) -> None:
        """Poll the spool forever. One session at a time; a poll that raises is
        logged and the loop continues — a daemon that dies on a bad record is a
        daemon that misses every wake after it."""
        interval = max(1.0, float(self.config.wake.poll_interval_sec))
        logger.info("wake runner up: spool %s, poll %.1fs",
                    self.spool.spool_dir, interval)
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("wake poll failed")
            await asyncio.sleep(interval)
