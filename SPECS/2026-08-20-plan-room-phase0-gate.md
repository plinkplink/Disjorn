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
  design intent). Ruled "Phase 0 Gate next" at seq 1391. Claudette's spec
  review at seq 1428; all six deltas (G1–G6) folded by Gable 2026-08-20 —
  provenance is this file's git history.

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
   act, not a violation — the count is the control. The count is derived,
   never stored (seq 1428 G5): computed at digest time from trailers in
   `main`'s history, so "counted forever" survives any database rebuild —
   the same cards-derive-from-artifacts rule the Plan Room spec is built on.
4. **Digest additions — the detector of record.** The custodian daily gains
   one drift block, opening with the gate's own liveness (item 5). Contents:
   mirror head; N commits on `main` since the last digest; M of them
   uncited; `classify_diff` run on every uncited commit, with an uncited
   Tier 2 posted as **LANE VIOLATION**, named; overrides-to-date; and a
   deploy-drift line comparing prod's running tree against mirror head.
   Since prod deploys from the mirror (plink, seq 1391), the hook already
   sits on the deploy path — the drift line is belt-and-braces, and it also
   catches the ship-by-not-publishing incentive Claudette named at seq 1380.
   **Citation is defined once, for both detectors (seq 1428 G1)**: a commit
   is cited if it carries a `review-seq`/`override-seq` trailer itself, OR
   is reachable from a trailer-bearing commit that entered `main` in the
   same digest window. The hook's unit is the push — the head's trailer
   covers its range; the digest applies the same rule over the
   new-since-last-digest set. A five-commit push with one trailer on the tip
   is one cited range, never one pass and four false violations. A detector
   that cries wolf on its own happy path gets muted in a week.
   **The digest resolves every cited seq (seq 1428 G2)** — the hook is dumb
   on purpose, so the validation it delegates has to actually exist here:
   the seq must resolve in the message store, must live in #custodian, and
   a `review-seq` whose author is the person who pushed is flagged
   **self-cited** — the comfortable failure mode, named so it can't pass as
   review. (`override-seq` is the pusher's own line by design; no self-cite
   check there.) Without this, `review-seq: 1` passes forever and the whole
   gate is a spelling test.
5. **The drift block opens with the detector's own liveness (seq 1428 G3).**
   First line: the installed hook path; the sha of the file the hook
   symlink actually resolves to; the mirror's sha for
   `harness/gatehouse/hooks/pre-receive-main-review`; or **ABSENT** if the
   symlink is missing or dangling. Install is hand-made, and
   committed-isn't-installed went four-for-four on 08-19/20 — an empty
   drift block must be distinguishable from a disarmed detector.

## Architecture notes
- Hook script in-tree at `harness/gatehouse/hooks/pre-receive-main-review`
  plus tests; install is one plink symlink into `disjorn.git/hooks/`
  (documented in an in-tree README line; exact host path known at the
  keyboard). The symlink points at a **deployed copy under the broker's
  tree, never a working clone** (seq 1428 G4) — a `git checkout` in a clone
  would silently disarm the gate; item 5's liveness line compares that
  deployed copy's sha against the mirror's. Compatibility: BUILD-LANE-V2 stage 2b names this same hook as
  part of the canonical-repo perimeter (seqs 1209/1212 — chown to broker
  user, drop resident group-write, pre-receive hook). This build delivers
  the hook 2b will install into its perimeter, so 2b installs rather than
  reinvents; nothing here blocks or presumes the rest of 2b.
- Digest: `harness/metrics/metrics.py` + its tests. Reads the same sources
  `harness/keyboard/board.py` already reads (mirror, SPECS, audit log),
  plus a message-store read to resolve cited seqs (G2) — it already posts
  the digest through a server path; reading a seq is the same privilege
  class or less. The deploy-drift comparison ships as a named importable
  function — `deploy_state()` — because the Plan Room's tri-state badge is
  the same computation and calls this function rather than re-implementing
  it (seq 1428 P6).
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

## Preconditions (named so the press doesn't discover them — both CLOSED
2026-08-20, seq 1428 G6)
- The canonical-objects chown: **discharged**, proven in action by the
  04:27Z rev build — res-gable cloned `disjorn.git` host-side and harvested
  into both bare repos cleanly (seqs 1421–1423). `verify` on the three
  repos stays on plink's list as record-keeping, not a gate.
- `SPECS/2026-08-19-read-repo-file-rev.md`: **merged (`2d8cb0d`) and
  deployed (seq 1429)**. Claudette's first `rev` call from her own seat is
  the liveness confirm; pre-merge review from her seat has branch vision.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1434
- **Confirmed at**: 8/20/2026
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`