"""The broker's own gate run for a loop/<slug> branch.

A green gate proves the run happened and was not skipped; it runs the branch's
own suite, so it is not a review. Only the four GATE lines decide anything —
everything else the run prints is log, kept at 0600 and never parsed.
`node_modules` is mounted read-only from the deployed tree, so a branch that
adds a dependency is typechecked and built without it and will read red until
the keyboard installs it.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

GATE_LINE_RE = re.compile(r"^GATE (tests|typecheck|build) (pass|fail|skipped)$")
GATE_EXIT_RE = re.compile(r"^GATE exit (\d+)$")
# Only pytest's own final summary line may move a count; a suite that prints
# "3 passed" in its own output is not a result. The whole line must be the
# summary, with or without the `=` rule that `-q` leaves off.
SUMMARY_RE = re.compile(
    r"^(?:=+ )?(?:\d+ \w+, )*(\d+) passed\b(?:, \d+ \w+)* in [\d.]+s", re.M)


@dataclass
class GateResult:
    tests: "bool | None"
    typecheck: "bool | None"
    build: "bool | None"
    exit_code: int
    log_path: str
    summary: str


def gates_json(r: GateResult) -> dict:
    """The classifier's `--gates` payload: a skipped client gate is true, and
    a gate that never ran is false, so a launch failure classifies
    fail-closed."""
    return {
        "tests": bool(r.tests),
        "typecheck": True if r.typecheck is None else bool(r.typecheck),
        "build": True if r.build is None else bool(r.build),
    }


def _write_log(log_dir: str, slug: str, body: str) -> str:
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    path = os.path.join(log_dir, f"gate-{slug}-{int(time.time())}.log")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return path


def _parse(stdout: str) -> "tuple[dict, int | None]":
    """A line that is absent stays absent here; the caller reads it as a fail."""
    seen: dict = {}
    exit_code = None
    for line in stdout.splitlines():
        line = line.strip()
        m = GATE_LINE_RE.match(line)
        if m:
            seen[m.group(1)] = {"pass": True, "fail": False,
                                "skipped": None}[m.group(2)]
            continue
        m = GATE_EXIT_RE.match(line)
        if m:
            exit_code = int(m.group(1))
    return seen, exit_code


def _summary(seen: dict, output: str) -> str:
    """The first summary line is the server suite, the second the harness one;
    the gate runs them in that order."""
    counts = SUMMARY_RE.findall(output)
    parts = []
    if seen.get("tests") is True and len(counts) >= 2:
        parts.append(f"server {counts[0]} passed; harness {counts[1]} passed")
    else:
        parts.append(f"tests {_word(seen.get('tests'))}")
    if seen.get("typecheck") is None and seen.get("build") is None:
        parts.append("client untouched")
    else:
        parts.append(f"client typecheck {_word(seen.get('typecheck'))}, "
                     f"build {_word(seen.get('build'))}")
    return "; ".join(parts)


def _word(value: "bool | None") -> str:
    if value is None:
        return "skipped"
    return "ok" if value else "FAILED"


def run_gates(argv_prefix: "list[str]", seat: str, slug: str, *,
              timeout: int, log_dir: str) -> GateResult:
    """A missing GATE line is a fail, never a pass; only a launch that never
    produced a process leaves the gates None."""
    argv = [*argv_prefix, seat, slug]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True,
                            timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        body = _decode(exc.stdout) + _decode(exc.stderr)
        return GateResult(False, False, False, 124,
                          _write_log(log_dir, slug, body), "timed out")
    except OSError as exc:
        return GateResult(None, None, None, 127,
                          _write_log(log_dir, slug, f"{argv}: {exc}\n"),
                          f"gate launch failed: {exc}")

    output = (cp.stdout or "") + (cp.stderr or "")
    log_path = _write_log(log_dir, slug, output)
    seen, reported = _parse(cp.stdout or "")
    exit_code = reported if reported is not None else cp.returncode
    return GateResult(
        tests=bool(seen.get("tests")),
        typecheck=seen["typecheck"] if "typecheck" in seen else False,
        build=seen["build"] if "build" in seen else False,
        exit_code=exit_code,
        log_path=log_path,
        summary=_summary(seen, output),
    )


def _decode(raw) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
