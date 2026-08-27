# Push-log coverage classes: stop the drift digest asserting an unmeasured cause

## Status
`merged`
<!-- built at the keyboard 2026-08-27 (Fable's tokens held back), landed as a
local commit on main — the exact class of commit this spec is about, so it
will read as `local-keyboard` in tomorrow's digest.

Detector (harness/metrics/metrics.py): parse_local_log + classify_coverage;
the block prints one `coverage above floor:` tally and names per-commit rows
only for `unexplained`, or for the informational classes when the section is
already alarming or verbose=True. Writer (harness/broker/brokerd.py):
_record_local_commit appends `LOCAL <ts> <sha> local-stamp` beside the push
log at every Status stamp. Config: [gate].local_log + [gate].local_committers,
added to the template AND to the live /etc/disjorn-broker/broker.toml.

Verified against live state for 2026-08-26: `coverage above floor: 67 commits
— covered 62, local-stamp 0, local-keyboard 5, unexplained 0`, no per-commit
rows, nothing contradicting the hook MATCH above it. Acceptance 4 (a new stamp
writing its own record) is pinned by the broker suite against the detector's
OWN reader, both halves of one grammar; the first real build after the broker
restart is what confirms it in production, and until one runs that is a test
result and not an observation.

Review owner claudette, unreviewed at the time of writing: not pushed to the
gatehouse, so no review-seq is claimed. -->

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2067
- **Confirmed at**: 8/27/2026
<!-- No Confirm record → no build. This is the gate. -->

## Lane
custodian / broker

## Review owner
claudette

## Problem

The daily GATE DRIFT section reports uncovered commits above the floor: N of M and prints, for each one, the fixed string entered main with no push-log line; the hook was absent or disarmed.

That string is a cause the digest never measured. In the 2026-08-27 00:05 digest it appeared five times, two lines below the same digest's own hook ... sha 7f8116f2 vs mirror 7f8116f2 (MATCH), with fail-open pushes in the log: 0 and overrides to date: 0. The digest contradicted itself.

All five commits were ones pre-receive cannot cover by construction:

- 1246c045, 5a08571e — the broker's own spec ## Status stamps, written as local commits directly into the canonical repo. Never pushed, so no hook, so no push-log line.
- 12b860c0, 70d26281, 5c8db463 — keyboard commits made directly in the canonical clone. Same reason.

The count is monotonic and growing: 2 of 32 on 08-24, 5 of 67 on 08-26. Every build and every keyboard session adds permanent rows. The failure mode is alarm fatigue — the day a genuinely uncovered pushed commit lands, it is one line among a dozen known-benign ones and nobody reads line six.

Non-goal: changing anyone's git workflow. Local commits into the canonical repo are legitimate and will continue. The detector is what's wrong, not the commits.

## Design

Classify every commit above the floor into exactly one class, and report the classes separately.

1. Broker self-stamp provenance. Wherever the broker makes a commit into the canonical repo on its own behalf (the spec Status stamp path is the known one), it appends a record to the push log — or to a sibling coverage log read by the same reader — naming the sha it just wrote, the timestamp, and the outcome word local-stamp. This is a positive record written by the actor, not an inference.

2. Classification at digest time. For each commit above the floor with no covering push-log range:

- has a local-stamp coverage record → class local-stamp
- no record, but committer/author matches a local system uid with no corresponding push entry → class local-keyboard
- otherwise → class unexplained

The literal string "the hook was absent or disarmed" is removed from the codebase. Nothing prints a hook state that was not measured on that line.

3. Reporting. The drift section prints a tally:

code

coverage above floor: 67 commits — covered 62, local-stamp 2, local-keyboard 3, unexplained 0


Only unexplained > 0 constitutes a drift finding and only that raises the section's alarm wording. local-stamp and local-keyboard are informational counts; their per-commit lines are printed only when the count is nonzero and the section is already alarming, or under a verbose flag — the default clean day is one line, not twelve.

The existing hook-sha MATCH/MISMATCH line, fail-open count and override count are unchanged and remain the real hook-state signals.

## Acceptance

1. On a day with a normal build plus keyboard commits and no tampering, the drift section reads clean: unexplained 0, nonzero local-stamp and/or local-keyboard, and no per-commit lines.
2. No output line asserts a hook state that contradicts the MATCH line above it. Grep for the removed string returns nothing outside this spec and its tests.
3. A synthetic commit placed on main above the floor with no push-log line, no stamp record and a non-local committer classifies as unexplained and the section alarms.
4. A broker Status stamp made after this lands produces a local-stamp record at stamp time, and the next digest classifies it without inference.
5. Backfill is not required. Commits already on main above the floor with no stamp record fall to local-keyboard or unexplained by rule 2; the five named above are expected to land as local-keyboard and that is the correct answer for them.
6. Server tests pass.

## Notes

Tier: this touches broker reporting and the broker's own commit path, not the write wall or any authorization decision. Classifier verdict to be taken at confirm.
