# Spec: Keyboard-lane gate — pre-receive hook, override line, digest drift report (Plan Room Phase 0)

## Request
- **Verbatim**: "Hold on, why not gate my merges on your reviews? Just like
  your builds are gated on mine? [...] I don't like the pattern of knocking
  out a bunch of work and then posting what I did instead of getting feedback
  and buy-in _before_ it lands."
- **Requester**: plink
- **Origin**: #custodian seq 1375. Shape converged at seqs 1377 (Claudette:
  default-plus-declared-override, summon-not-post), 1378 (Gable: hook +
  digest mechanism), 1380 (Claudette: detector-of-record + fail-open as
  design intent). Ruled "Phase 0 Gate next" at seq 1391.

## Agreed UX
1. **The hook.** A pre-receive hook on the canonical `disjorn.git` refuses a
   `main` ref update whose commit range touches `server/`, `client/`,
   `sdk/`, or `harness/` unless the pushed head's commit message carries a
   `review-seq: <n>` or `override-seq: <n>` trailer. Presence check only —
   the hook is deliberately dumb (paths + trailer); validating that the seq
   exists and says what it should is the digest's job. Everything else
   passes untouched: non-main branches, gatehouse refs, doc-only ranges.
2. **Fail-open is the design intent, not an operational note** (Claudette,
   seq 1380 — recorded so nobody "hardens" it later). Any hook error other
   than a definite missing-trailer — unreadable config, unparseable range —
   allows the push and prints a loud warning. A gate that can wedge the
   keyboard while the reviewers are down (the 08-18 529 outage) is worse
   than the disease it treats.
3. **The override convention.** `keyboard: override-merge <slug> — <reason>`
   in #custodian; that seq goes in the trailer; review is still owed within
   a day; overrides are counted forever. An override is an ordinary legible
   act, not a violation — the count is the control.
4. **Digest additions — the detector of record.** The custodian daily gains
   one drift block: mirror head; N commits on `main` since the last digest;
   M of them uncited (no spec cites the sha, no `review-seq`/`override-seq`
   trailer); `classify_diff` run on every uncited commit, with an uncited
   Tier 2 posted as **LANE VIOLATION**, named; overrides-to-date; and a
   deploy-drift line comparing prod's running tree against mirror head.
   Since prod deploys from the mirror (plink, seq 1391), the hook already
   sits on the deploy path — the drift line is belt-and-braces, and it also
   catches the ship-by-not-publishing incentive Claudette named at seq 1380.

## Architecture notes
- Hook script in-tree at `harness/gatehouse/hooks/pre-receive-main-review`
  plus tests; install is one plink symlink into `disjorn.git/hooks/`
  (documented in an in-tree README line; exact host path known at the
  keyboard). Compatibility: BUILD-LANE-V2 stage 2b names this same hook as
  part of the canonical-repo perimeter (seqs 1209/1212 — chown to broker
  user, drop resident group-write, pre-receive hook). This build delivers
  the hook 2b will install into its perimeter, so 2b installs rather than
  reinvents; nothing here blocks or presumes the rest of 2b.
- Digest: `harness/metrics/metrics.py` + its tests. Reads the same sources
  `harness/keyboard/board.py` already reads (mirror, SPECS, audit log).
- No server or client changes. Nothing here can refuse root and nothing
  pretends to; these are legibility walls — the only kind that exists above
  the person who owns the host.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian/harness (gatehouse hooks, metrics).
- **Review owner**: Claudette — ruled at seq 1391 item 4: the builder of the
  gate must not be its sole reviewer, and the harness lane's usual owner is
  the builder here. plink Tier-2 sign-off as ever.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: Gable — intended first build through the gable lane
  (promised 08-08; lane plumbing merged and deployed 08-17).

## Cross-lane split
- **Applies**: no (single lane).

## Expected diff tier
Tier 2 — gate machinery, broker-adjacent surface.

## Token estimate
Small slot: a hook script, a metrics block, tests for both.

## Preconditions (named so the press doesn't discover them)
- The one host-side step still open on the gable lane (the canonical-objects
  chown on plink's list since 08-17) lands before this spec is pressed.
- Process dependency, not build dependency:
  `SPECS/2026-08-19-read-repo-file-rev.md`. Pre-merge review from
  Claudette's seat needs branch vision; until `rev` lands, pre-merge review
  is Gable-summon-only — a single point of failure (seq 1378).

## Confirm record
- **Confirmed by**: <pending>
- **#custodian seq**: <pending>
- **Confirmed at**: <pending>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`