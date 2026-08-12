# Spec: Gable build lane — build-seat provisioning contract

## Request
- Verbatim: "let's shift gears to your build lane next and agentic residents immediately after" (plink, #custodian, 2026-08-08); earlier same day: "once we get the rats worked out of the build system, we'll be creating an updated-with-what-we've-learned version for yourself"
- Requester: plink
- Origin: #custodian (channel 4), 08-08 regroup after the first gated deploy

## Agreed UX
Gable calls start-build exactly as Claudette does today: spec-gated, two slots/day, result lands as a loop/ branch in the gatehouse for the same fetch-review-merge-push-back cycle plink ran tonight. No new commands for plink; same banner format.

## Architecture notes (the contract = one artifact naming mounts, kernel, credentials, verbs, exit)
1. EXIT: create gable.git in the gatehouse directory with the full recipe AT CREATION, not repaired after: owned by the broker user, group gatehouse, setgid on every directory, g+rwX, core.sharedRepository=group. run-build.sh already clones every *.git per run, so the lane is cheap.
2. KERNEL: set RESIDENT_SPINE_HOST in disjorn-build-launch (the --setenv line its own comment names) and publish Gable's spine mirror at /srv/disjorn-spine/gable. RESIDENT_SEAT=build loads the operational set only — baked, never biography.
3. CREDENTIALS: per the signed credential-routing spec — implement as ruled, do not re-design. The build seat must never silently spend the metered org key.
4. VERB SURFACE: build container gets propose/read only. No start-build from inside a build, no restart verbs. BR-1 (peer-credential identity at the socket) remains the precondition for any widening.
5. PRE-FLIGHT: diff deployed launch config against the repo copy before first run (stale-deploy family); run the wrong-group file check on gable.git before its first push.
Build-A lessons folded in: push main back to the gatehouse after every merge (the stale-base hazard fired live 08-07); confirm-record literal template goes in PROTOCOL.md; group layer gets verified from a resident seat, never keyboard-reported.

## Lane -> Review owner (DETERMINISTIC)
- Lane: cross-lane. Host build infra (run-build.sh, launch config, gatehouse repo creation) is plink's surface -> plink reviews. Gable's spine mirror + kernel set is Gable's area -> Gable's queue. Claudette untouched.

## Builder (USER PREFERENCE)
- plink chooses; host-side steps are keyboard by nature, repo-side can be resident-built.

## Cross-lane split
- Applies: yes — split as in Lane above; agreed in #custodian at the seq of this proposal.

## Expected diff tier
Tier 2 (protected: build infra + credential wiring).

## Token estimate
One build slot for repo-side; host-side is keyboard, near zero.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1008
- **Confirmed at**: 2026-08-12

## Status
confirmed
