"""Get a report to #custodian — or, in dry-run, to stdout.

Posting is the ONLY side effect this package has, and it is not a store write:
it hands proposal text to the broker's `file-proposal` verb, which posts to
#custodian *as the broker's own bot identity*. The resident supplies data; the
broker supplies the authority to post (PROTOCOL.md). One proposal = one
`file-proposal` call, so each is independently reviewable in the channel
(who decides cuts = who decides adds).

The broker CLI is invoked as a subprocess (`broker file-proposal --text ...`).
The runner is injectable so tests drive a fake CLI and NOTHING touches the real
socket. In the build worktree only `--dry-run` is ever used.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional

from consolidation.config import ConsolidationConfig
from consolidation.model import ConsolidationReport

# 1..4000 chars is the file-proposal contract; keep a margin under 4000.
PROPOSAL_TEXT_CAP = 3900

# A runner takes an argv list and returns (returncode, stdout, stderr).
Runner = Callable[[list[str]], "subprocess.CompletedProcess"]


@dataclass
class PostOutcome:
    dry_run: bool
    posted: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    text: str = ""  # dry-run: the full rendered report

    @property
    def ok(self) -> bool:
        return self.failed == 0


def _default_runner(argv: list[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def post_report(
    report: ConsolidationReport,
    cfg: ConsolidationConfig,
    *,
    dry_run: bool,
    runner: Optional[Runner] = None,
    out=None,
) -> PostOutcome:
    """Dry-run prints the whole report and posts nothing. Real run posts each
    proposal via the broker file-proposal CLI."""
    if dry_run:
        text = report.render_full()
        if out is not None:
            out.write(text)
        return PostOutcome(dry_run=True, text=text)

    runner = runner or _default_runner
    outcome = PostOutcome(dry_run=False)
    header = report.batch_header()

    for i, proposal in enumerate(report.proposals, 1):
        body = (
            f"{header}\n"
            f"[item {i}/{len(report.proposals)}]\n\n"
            f"{proposal.render()}"
        )
        if len(body) > PROPOSAL_TEXT_CAP:
            body = body[:PROPOSAL_TEXT_CAP].rstrip() + "\n…(truncated)"
        argv = [cfg.broker_cli, "file-proposal", "--text", body]
        try:
            result = runner(argv)
        except Exception as exc:  # transport / missing CLI / timeout
            outcome.failed += 1
            outcome.errors.append(f"proposal {i}: runner raised {type(exc).__name__}: {exc}")
            continue
        if result.returncode == 0:
            outcome.posted += 1
        else:
            outcome.failed += 1
            detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
            outcome.errors.append(
                f"proposal {i}: broker exit {result.returncode}: {detail[:200]}"
            )
    return outcome
