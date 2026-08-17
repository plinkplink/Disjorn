[proposal from res-gable] SPEC DRAFT (from SPECS/TEMPLATE.md, condensed to fit this verb) — File-vision: every branch in the mirror, one branch in the build seat. Requester: plink; origin: #custodian 2026-08-15, the file-vision question. Read at mirror head b1db377.

AGREED UX: (1) refresh-mirror also fetches every entitled gatehouse repo's branches into the RO mirror under refs/gatehouse/<repo>/*, with --prune — a vanished ref means harvested-or-deleted, and the banner says which (pairs with seq 1255 narration). (2) The "ping" is not a new verb — it falls out free: git -C /opt/disjorn rev-parse gatehouse/disjorn/loop/<slug>:<path> compared to the same path at main answers "did their side change" in one command, zero file reads, zero broker calls. One documented line in resident docs. (3) refresh_mirror added to both residents' tool schemas — already ON in verbs.toml for both, the drift is schema-side, fourth instance; proper fix is generating the schema from verbs.toml. (4) A build workspace clone sees main plus its own loop/<slug> only: --single-branch on the provisioning clone at run-build.sh:287. (5) After a harvest prints PUBLISHED, the wrapper runs the same mirror fetch host-side before the reaper banners — a banner may never name a sha the audience cannot open.

ARCHITECTURE: brokerd.py _verb_refresh_mirror keeps today's ff-only main update from origin (plink's working clone = deploy truth) and adds one fixed-argv fetch per gatehouse repo. Zero caller args stays: a resident can refresh but never aim git. Namespaces disjoint: refs/remotes/origin/* is incidental, refs/gatehouse/* is deliberate. NOTE, measured: the mirror ALREADY leaks a partial stale branch view — origin/loop/* and even origin/worktree-agent-* ride the default fetch refspec, unpruned. Branch-hiding is not currently enforced; it is merely unreliable. This spec replaces accidental partial vision with deliberate complete vision. The quarantine reachability check asks the gatehouse, not the workspace, so --single-branch does not touch it; seq-1236's glob widening stays separate and still wanted. Quarantine piles stay invisible by design: evidence, not candidates (same boundary class as Claudette's detached-HEAD note).

LANE -> REVIEW OWNER (deterministic): custodian — brokerd.py, run-build.sh, mirror config -> Gable. CROSS-LANE: yes — Claudette's tool schema is her surface -> her review queue; mine -> mine. Split stated here for the confirm to witness.

TIER: 2 advisory (broker + wrapper are protected surfaces). TOKENS: one small build slot — fetch refspec, one clone flag, harvest hook, schema line, tests.

DECISION POINT flagged, not baked in: two sources (origin for main, gatehouse for branches) versus pointing everything at the gatehouse. I prefer two-source: mirror main must equal the main prod actually runs, and push-back can lag the merge.

CLOSES: the structural half of 1226 (residents review loop/* pre-merge — which kills the crossing mechanism, not just labels it), the refresh-mirror schema drift, and the read-main-twice-to-detect-motion token burn that started tonight's economics.

## Shake-out (2026-08-15 — Claudette cross-lane review + plink confirm, folded)
- Decision point RESOLVED: two-source. plink 08-15: "I agree with both of you on two sources vs just gatehouse." Claudette second reason recorded: single-source would make mirror-main LEAD prod — the merged-is-not-deployed gap inverted, the direction nobody watches.
- Item 3 upgraded per review: the build GENERATES both residents tool schemas from verbs.toml; no hand-added schema line. Kills the drift class (four instances), not the instance.
- Item 4 stated cost, accepted: under --single-branch a quarantined workspace carries only main plus its own slug, so a pile holds less divergence evidence exactly when one is being read. The gatehouse retains the full picture.
- Item 2 scope note: the rev-parse ping is executable today only from shell-bearing seats (Gable, keyboard). Claudette execution path — read_repo_file with a rev argument and a sha-only mode — is deferred to the tools discussion (plink 08-15); until it lands, the read-main-twice CLOSES claim is half-closed for her seat.
- Tier-2 rationale, promoted from margin per review: branch-hiding is not a property being surrendered — it never held (origin/loop/* and origin/worktree-agent-* already ride the default refspec, unpruned). Deliberate complete vision replaces accidental partial vision.
- Landing-step self-reference corrected: this file (2026-08-14-file-vision.md) IS the landing; the seq-1267 line naming an 08-15 filename is superseded here.
- Cross-lane concur: Claudette, #custodian 2026-08-15 — yes to all five with the above folded.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1272
- **Confirmed at**: 2026-08-15

## Status
merged
<!-- advanced from `confirmed` by `board --mark-merged` on 2026-08-17: build merged as c20eceb. The word `confirmed` on a merged spec made it indistinguishable from a buildable one. -->
