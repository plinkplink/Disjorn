[proposal from res-gable] SPEC DRAFT (from SPECS/TEMPLATE.md, condensed to fit this verb) — File-vision: every branch in the mirror, one branch in the build seat. Requester: plink; origin: #custodian 2026-08-15, the file-vision question. Read at mirror head b1db377.

AGREED UX: (1) refresh-mirror also fetches every entitled gatehouse repo's branches into the RO mirror under refs/gatehouse/<repo>/*, with --prune — a vanished ref means harvested-or-deleted, and the banner says which (pairs with seq 1255 narration). (2) The "ping" is not a new verb — it falls out free: git -C /opt/disjorn rev-parse gatehouse/disjorn/loop/<slug>:<path> compared to the same path at main answers "did their side change" in one command, zero file reads, zero broker calls. One documented line in resident docs. (3) refresh_mirror added to both residents' tool schemas — already ON in verbs.toml for both, the drift is schema-side, fourth instance; proper fix is generating the schema from verbs.toml. (4) A build workspace clone sees main plus its own loop/<slug> only: --single-branch on the provisioning clone at run-build.sh:287. (5) After a harvest prints PUBLISHED, the wrapper runs the same mirror fetch host-side before the reaper banners — a banner may never name a sha the audience cannot open.

ARCHITECTURE: brokerd.py _verb_refresh_mirror keeps today's ff-only main update from origin (plink's working clone = deploy truth) and adds one fixed-argv fetch per gatehouse repo. Zero caller args stays: a resident can refresh but never aim git. Namespaces disjoint: refs/remotes/origin/* is incidental, refs/gatehouse/* is deliberate. NOTE, measured: the mirror ALREADY leaks a partial stale branch view — origin/loop/* and even origin/worktree-agent-* ride the default fetch refspec, unpruned. Branch-hiding is not currently enforced; it is merely unreliable. This spec replaces accidental partial vision with deliberate complete vision. The quarantine reachability check asks the gatehouse, not the workspace, so --single-branch does not touch it; seq-1236's glob widening stays separate and still wanted. Quarantine piles stay invisible by design: evidence, not candidates (same boundary class as Claudette's detached-HEAD note).

LANE -> REVIEW OWNER (deterministic): custodian — brokerd.py, run-build.sh, mirror config -> Gable. CROSS-LANE: yes — Claudette's tool schema is her surface -> her review queue; mine -> mine. Split stated here for the confirm to witness.

TIER: 2 advisory (broker + wrapper are protected surfaces). TOKENS: one small build slot — fetch refspec, one clone flag, harvest hook, schema line, tests.

DECISION POINT flagged, not baked in: two sources (origin for main, gatehouse for branches) versus pointing everything at the gatehouse. I prefer two-source: mirror main must equal the main prod actually runs, and push-back can lag the merge.

CLOSES: the structural half of 1226 (residents review loop/* pre-merge — which kills the crossing mechanism, not just labels it), the refresh-mirror schema drift, and the read-main-twice-to-detect-motion token burn that started tonight's economics.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1272
- **Confirmed at**: 2026-08-15

## Status
confirmed