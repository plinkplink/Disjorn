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
  review at seq 1428; all six deltas (G1–G6) folded by Gable 2026-08-20.
  Claudette's seq 1433 follow-up (G1b: push-log over reachability inference)
  folded same day, replacing the original G1 fold; her seq 1446 follow-up
  (G1c: the uncovered flag needs a genesis floor) folded same day; her
  seq 1450 follow-up (G1d: floor provenance + digest-pinned floor motion)
  folded same day — provenance is this file's git history.

## Agreed UX
1. **The hook.** A pre-receive hook on the canonical `disjorn.git` refuses a
   `main` ref update whose commit range touches `server/`, `client/`,
   `sdk/`, or `harness/` unless the pushed head's commit message carries a
   `review-seq: <n>` or `override-seq: <n>` trailer. Presence check only —
   the hook is deliberately dumb (paths + trailer); validating that the seq
   exists and says what it should is the digest's job. Everything else
   passes untouched: non-main branches, gatehouse refs, doc-only ranges.
   **The hook also writes what it saw (seq 1433 G1b)**: one appended line
   per `main` push decision — timestamp, `old..new`, trailer found or
   `NONE`, and outcome (`passed` / `refused` / `failed-open`) — to an
   append-only log beside the hook in the canonical repo's git-dir.
   **The log opens with a genesis line (seq 1446 G1c), and the line
   names its own provenance (seq 1450 G1d)**: the install step seeds
   `GENESIS seeded` — timestamp plus `main`'s head at install — so the
   floor predates the first push. If the hook finds no log, the first
   line it writes — before that push's decision line — is
   `GENESIS lazy`, timestamp plus the `old` sha of the triggering push.
   Either way the floor exists before any coverage line does, and its
   *existence* never depends on a hand step being remembered — this
   week's install record argues against trusting one. But the two
   births are not equivalent and must not read alike: a lazy floor is
   minted from whatever `main` looked like the first time the hook
   happened to fire, which proves nothing about the window between
   install and that firing. A lazy genesis is therefore a liveness-line
   condition (item 5), never a healthy default — the hand step's
   forgetting becomes loud instead of load-bearing. Writing the line is
   part of the fail-open envelope: a log write that errors warns loudly
   and never blocks the push.
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
   **Citation is defined once, from push truth (seqs 1428 G1, 1433 G1b)**:
   a commit is cited iff it is covered by a logged push (item 1's
   append-only log) whose trailer resolves per G2. The digest reads push
   boundaries from the log the hook already wrote, never reconstructs them
   from reachability. A five-commit push with one trailer on the tip is one
   cited range, never one pass and four false violations — and the
   laundering hole the original G1 fold opened is closed: an uncited push
   that landed only because the hook **failed open** stays uncited forever;
   a later trailer-bearing push in the same window cannot retroactively
   bless its ancestors, which was exactly the case this detector exists to
   catch. Two facts fall out that exist nowhere else: the fail-open count
   (how often the escape hatch actually fired), and **uncovered commits** —
   anything that entered `main` **after the genesis floor** with no
   covering log line arrived while the hook was absent or disarmed (seq
   1446 G1c). The scope of "before the floor" bends with the floor's
   provenance (seq 1450 G1d): below a **seeded** floor is out of scope
   by agreement — the gate starts where the log starts, the first
   digest after install flags nothing historical, and the detector
   doesn't cry wolf on its first breath. Below a **lazy** floor is not
   out of scope, it is **unverifiable**, and the block says so — a hook
   disarmed between install and its first firing mints the floor on the
   far side of exactly the window the uncovered flag exists to catch,
   so nothing below a lazy floor may read as clean. The genesis line
   also arms two tamper tells: an append-only log with a **second**
   genesis line was deleted and recreated, and a log whose first line is
   not a genesis line was truncated rather than merely young — both are
   liveness-line material (item 5), louder than any single uncovered
   commit. Both tells need surviving lines, though: a log deleted whole
   and lazily re-birthed shows one clean genesis and a floor that
   silently advanced past everything the old log covered (seq 1450
   G1d) — that case is caught outside the git-dir, by item 5's
   floor-motion check against the previous digest post. Uncovered stays
   the loudest per-commit flag in the block; it closes G3's blind spot
   from the detection side too. If the log itself is absent or
   unreadable, the liveness line says so, the digest falls back to
   strict per-commit trailer presence — no reachability inference — and
   the floor-motion check still runs, because its baseline lives in the
   message store: a lost log degrades to more flags, never fewer, now
   including the whole-log deletion that would otherwise read as a
   merely young one.
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
   symlink is missing or dangling. Second line: the push log's genesis
   state — floor sha, date, and provenance (`seeded` / `lazy`), or
   `TRUNCATED` (first line isn't genesis), or `REPLACED` (more than one
   genesis line), or `NO LOG` (seq 1446 G1c). A `lazy` floor renders as
   a warning, never a plain state: "floor minted at first push; commits
   before `<sha>` unverifiable" (seq 1450 G1d). Third line, the one no
   git-dir event can clobber: the digest reports the floor in every
   drift block and compares it against the floor its own previous
   digest post reported — any motion, in either direction, is flagged
   **FLOOR MOVED**, the loudest line in the block. Floors don't move;
   a moved floor means the log was replaced, whatever the log itself
   claims. The baseline lives in the message store — outside the
   git-dir, beyond the reach of a log delete or a repo re-create — so
   this is the tell that survives when both in-log tamper tells die
   with the log. Install is hand-made, and committed-isn't-installed
   went four-for-four on 08-19/20 — an empty drift block must be
   distinguishable from a disarmed detector.

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
  plus a message-store read to resolve cited seqs (G2), plus the hook's
  push log (G1b). The push log is not a rebuildable cache and doesn't
  violate G5's derived-never-stored rule: push boundaries and fail-open
  firings exist nowhere in git and cannot be derived after the fact — it's
  a primary record, same class as the broker audit log. The floor-motion
  check (G1d) adds no storage anywhere: its baseline is the floor sha in
  the digest's own previous #custodian post, read through the
  message-store access G2 already grants — derived at digest time,
  consistent with G5. The first digest after install has no baseline and
  says so; it reports the floor it will then hold every digest after.
  The override count
  stays derived from `main`'s trailers; the log never becomes the source
  for "counted forever." The deploy-drift comparison ships as a named importable
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

## Preconditions — both CLOSED 2026-08-20 (seq 1428 G6)
Named so the press doesn't discover them.
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
`confirmed`