# Spec: 08-gatehouse-repo.sh — fix the two false-FAIL checks and the HEAD birth defect

## Request
- **Verbatim**: "You want to pick something easy from the backlog or something that's been bothering you to build?" (plink) — target per Claudette: "ideally the 08 preflight's bogus 444 check, so the acceptance test and a real fix are the same commit."
- **Requester**: plink
- **Origin**: #custodian (channel 4), 2026-08-12 evening — the build-lane preflight session.

## Agreed UX
`sudo bash 08-gatehouse-repo.sh verify <repo> <resident...>` passes clean on a
healthy gatehouse repo. Today it cannot: two of its checks assert group-WRITE
on loose objects, which git never grants — under `core.sharedRepository=group`
a loose object is written 0444 by design. A healthy repo FAILs forever and the
caveat lives in chat memory instead of the check. After this build, verify
asserts what git actually promises, and also catches the HEAD→master birth
defect instead of leaving it to a hand-run one-liner.

## Architecture notes
One file: `harness/keyboard/08-gatehouse-repo.sh`. Three changes.

1. **Global sweep (line ~227)** — `find "$REPO" ! -perm -020` flags every
   loose/pack object. Split it: prune `objects/` from the group-write sweep;
   add an objects sweep asserting group-READ on files (`! -perm -040` → FAIL)
   and setgid+group-write on the fan-out directories. Mutable paths (HEAD,
   config, refs/, packed-refs, info/) keep the group-write assertion.
2. **Seat probe (line ~290)** — after `hash-object -w` succeeds as res-<r>,
   the probe demands the new object be group-writable. Wrong promise: assert
   group == gatehouse group (unchanged) and group-READ (mode 444 expected).
   Write capability is already proven by hash-object succeeding.
3. **HEAD birth defect (line ~158)** — `git init --bare --shared=group` with
   no `-b` leaves HEAD → refs/heads/master while the lanes push main.
   `create`: add `-b main`. `verify`: add a check that
   `git symbolic-ref HEAD` = refs/heads/main.

Acceptance: verify run on gable.git (known healthy: group/setgid PASSED
08-08) reports zero FAILs, and 09-build-lane-preflight §2 inherits the green.
No refs, objects, or hooks are touched by verify — that contract (header,
lines 11–12) is unchanged.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — `harness/keyboard/` is plink's keyboard surface.
- **Review owner**: plink

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: Gable — this is the acceptance test of the gable build lane.

## Expected diff tier
Tier 1 (auto-apply + posted diff) expected — single harness script, no prod
server code, no sudoers. Classifier gates the actual result at merge.

## Token estimate
Small. Single-file bash edit; one build slot.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1128
- **Confirmed at**: 8/12/2026
<!-- No Confirm record → no build. This is the gate. -->

## Status
confirmed
