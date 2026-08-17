# Spec: Build publish path — host-side harvest, banner from measurement, no gatehouse in the container

<!--
Assembled at the keyboard 2026-08-13 from the spec text Gable posted at
#custodian seq 1173, with Claudette's seq 1175 amendments integrated where
they land (marked [1175] inline). Filed after Gable's seq 1213 finding that
the spec had never been transcribed to a file — every "sitting at draft"
statement made in-channel referred to a file that did not exist. This file
is the state of record from here on. Root cause the spec addresses is the
2026-08-13 keyboard finding (gatehouse-repair report + seq 1170): podman
`--userns keep-id` does not map supplementary groups, so the `gatehouse`
group does not exist inside build containers, and in-container pushes work
only by uid-ownership accident.
-->

## Request
- **Verbatim**: "Now is the time to refactor if this setup is not salvageable, or there is a better workaround to the ambiguous ownership problem."
- **Requester**: plink
- **Origin**: #custodian (channel 4), seq 1166, 2026-08-13

## Agreed UX
- The build session commits to `loop/<slug>` in its workspace clones and
  never pushes. `git push` disappears from its kernel and the gatehouse
  from its world.
- After the container exits cleanly, the wrapper — host-side, already
  running as res-`<name>`, where the gatehouse group and the
  `/etc/gitconfig` exemptions actually work — publishes `loop/<slug>` from
  each entitled workspace clone into the matching gatehouse repo and prints
  one machine-readable line per repo: `PUBLISHED <repo>.git <sha>` or
  `PUBLISH-FAILED <repo>.git <verbatim git error>`.
- The done-banner is derived from those lines and nothing else: it names
  the repo and the sha, or it says FAILED with the error. A build that
  produced no commits gets its own honest line, not a phantom branch. "On
  the branch for review" without a measured sha becomes impossible to
  print.

## Architecture notes
All in the disjorn tree; the load-bearing seams by file:

1. `harness/cc/run-build.sh` —
   (a) provisioning loop (~line 197): clone the **entitled set only** —
   `disjorn.git` + `<name>.git` — fail loud if either is missing
   (Claudette's argument, 08-12/13). [1175] The origin rewrite to
   `/run/gatehouse` is **deleted entirely, not repointed** — with the mount
   gone, an origin aimed at a path the container can't see is a trap;
   "no such remote" is a better error than a path that looks real.
   (b) Drop `-v "$GATEHOUSE:/run/gatehouse"` (~line 243) — no writable
   path out of the container remains.
   (c) Post-exit harvest: the trailing `exec podman` (~line 645) becomes
   foreground-run-then-harvest. [1175] **Killing the wrapper must kill the
   container**: with `exec` gone the wrapper is a parent, and a killed
   parent can leave a container writing into `work/<repo>` while the next
   launch deletes it — an explicit trap does `podman rm -f` on every exit
   path, with a test that proves it. A timeout-killed wrapper skips
   harvest *by design*, and that skip must surface as a FAILED banner,
   never silence. On clean exit, for each entitled repo whose workspace
   clone has commits on `loop/<slug>` beyond the clone point: publish into
   `$GATEHOUSE/<repo>.git` (fetch-into or host-path push — builder's
   call; the 08-13 rescue proved the push variant), **no force,
   non-fast-forward refused loudly**, then rev-parse *in the gatehouse*
   and print the `PUBLISHED` line.
   (d) [1175] **Quarantine clause — harvest failure makes the workspace
   clone undeletable.** Provisioning must refuse to `rm -rf` a clone
   holding commits on a `loop/*` branch that are not in the gatehouse:
   move it to a quarantine path and fail loud, or refuse to launch. The
   08-13 rescue existed only because a human posted a warning and nobody
   launched; a human remembering is not a design. The killed-container
   test asserts a quarantined clone, not a clean slate.
2. `harness/cc/build-kernel.md` — delete the push instruction; the
   session's contract ends at "commit to `loop/<slug>`."
3. `harness/broker/brokerd.py` reaper — banner text built from the
   `PUBLISHED`/`PUBLISH-FAILED` lines in the wrapper's output; absent
   lines = FAILED banner. **No separate verification path** — the harvest
   is the verification (one mechanism; two can disagree).
4. `harness/cc/BUILD-SEAT-CONTRACT.md` — the "one writable path out"
   clause becomes "no writable path out; the product leaves via the
   wrapper's harvest."
5. [1175] **Multi-owner objects are by design once seats publish
   host-side into repos they don't own** — `disjorn.git/objects` will hold
   res-gable and res-claudette files side by side, and that is fine
   precisely because the 08-12 verify fix asserts group-*read* on objects,
   not group-write. Written here so nobody reads mixed owners as drift in
   three weeks and chowns it.
6. Tests: entitled set (missing entitled repo → loud fail; foreign repo
   present → not cloned), harvest (published sha equals workspace sha;
   non-ff refused; zero-commit build → no branch plus honest line; killed
   container → FAILED banner + quarantined clone), banner derivation from
   the lines, wrapper-kill kills container.

Sequenced consequence, **not in this build**: re-running `create` to
converge claudette.git and disjorn.git to recipe ownership stays blocked
until this merges and Claudette's next build proves the harvest path —
today her lane publishes only via the drift this spec retires. (The wider
2b prerequisite — chown to broker user, drop resident group-write from
canonical, pre-receive hook — is recorded at #custodian seq 1209/1212 and
in BUILD-LANE-V2.md; it queues behind this spec, not inside it.)

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — harness/cc + harness/broker, the keyboard/harness
  surface.
- **Review owner**: plink.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: the keyboard (plink's pick, 2026-08-13 — "weapons-free",
  subagent orchestration). Direct build; does not run through the broker
  lane this spec repairs.

## Cross-lane split
- **Applies**: no.

## Expected diff tier
Tier 2 (wrapper + broker are protected surfaces; advisory — classifier
gates at merge).

## Token estimate
Comparable to the 08-12 verify-fixes build: one wrapper, one reaper
function, kernel text, contract doc, tests.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1211
- **Confirmed at**: 2026-08-13

## Status
merged
<!-- advanced from `confirmed` by `board --mark-merged` on 2026-08-17: build merged as 75a5dbb. The word `confirmed` on a merged spec made it indistinguishable from a buildable one. -->
