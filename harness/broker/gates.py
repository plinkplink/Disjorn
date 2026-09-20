#!/usr/bin/env python3
"""The broker's own gate run: suite, typecheck and build, launched by the broker
and measured from the launcher's stdout.

PLACEHOLDER ON THIS BRANCH — the GATES hand owns this module; only the shapes
`brokerd.py` imports are fixed here, and the keyboard drops this file whole at
integration. Keep the dataclass fields and the two signatures identical.
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import tempfile
from typing import Optional

GATE_KINDS = ("tests", "typecheck", "build")
_GATE_LINE_RE = re.compile(r"^GATE\s+(tests|typecheck|build)\s+(pass|fail|skipped)\s*$")
_GATE_EXIT_RE = re.compile(r"^GATE\s+exit\s+(\d+)\s*$")


@dataclasses.dataclass
class GateResult:
    """None means the gate produced no verdict: not run, or skipped."""

    tests: Optional[bool]
    typecheck: Optional[bool]
    build: Optional[bool]
    exit_code: int
    log_path: str
    summary: str


def parse_gate_lines(stdout: str) -> dict:
    """The verdicts and the exit code, read ONLY from the `GATE` lines.

    A missing line is a fail, so a launcher that dies mid-run never reads green."""
    verdicts: dict = {k: False for k in GATE_KINDS}
    exit_code = 1
    for line in (stdout or "").splitlines():
        hit = _GATE_LINE_RE.match(line.strip())
        if hit:
            verdicts[hit.group(1)] = (None if hit.group(2) == "skipped"
                                      else hit.group(2) == "pass")
            continue
        hit = _GATE_EXIT_RE.match(line.strip())
        if hit:
            exit_code = int(hit.group(1))
    verdicts["exit_code"] = exit_code
    return verdicts


def _summary(verdicts: dict) -> str:
    parts = []
    for kind in GATE_KINDS:
        v = verdicts[kind]
        parts.append(f"{kind} {'skipped' if v is None else 'ok' if v else 'FAILED'}")
    return "; ".join(parts)


def run_gates(argv_prefix: list[str], seat: str, slug: str, *,
              timeout: int, log_dir: str) -> GateResult:
    """Run the gates synchronously and write the whole run to a 0600 log."""
    argv = [*argv_prefix, seat, slug]
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    fd, log_path = tempfile.mkstemp(prefix=f"{slug}-", suffix=".log", dir=log_dir)
    os.close(fd)
    os.chmod(log_path, 0o600)
    try:
        cp = subprocess.run(  # noqa: S603 — argv list, no shell
            argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(f"timed out after {timeout}s\n")
        return GateResult(None, None, None, 124, log_path, "timed out")
    except OSError as exc:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(f"{exc}\n")
        return GateResult(None, None, None, 127, log_path,
                          f"the gate runner would not start ({exc})")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(cp.stdout or "")
        fh.write(cp.stderr or "")
    verdicts = parse_gate_lines(cp.stdout or "")
    return GateResult(verdicts["tests"], verdicts["typecheck"], verdicts["build"],
                      verdicts["exit_code"], log_path, _summary(verdicts))


def gates_json(r: GateResult) -> dict:
    """The classifier's `--gates` object: a skipped gate is green, a gate that
    never ran is red."""
    return {kind: (True if getattr(r, kind) is None and r.exit_code == 0
                   else bool(getattr(r, kind)))
            for kind in GATE_KINDS}
