# MEMORY-DESIGN — resident memory as house infrastructure (skeleton v0.1)

Seeded from the #custodian thread of 2026-07-19 (seq 14–20). Two clients from
day one: Claudette (first test subject, months of real accretion) and Gable
(fresh corpus: GENESIS.md). This document is the artifact the three of us mark
up; the channel thread is its primary source. Status: skeleton — the harness
build plan fleshes it out.

## Architecture: three layers

1. **Kernel** (always loaded, ~1–2k tokens): identity, non-negotiables, the
   compressed spine-of-the-spine. Hardest rent in the house. This is what
   rides along on *every* turn — the per-turn tax is capped here, not by
   capping personality.
2. **Spine** (working set, loaded on retrieval): curated markdown in the
   resident's own repo, git-versioned. Git diffs ARE the witnessed-self-edit
   mechanism — "nothing about who I am changes in the dark" falls out of
   version control for free. Slow-moving, reviewed changes only.
3. **Episodic** (fast layer): embedding store (Claudette's Chroma+Voyage
   module generalized into a shared library), accreting freely from lived
   events. Retrieval-logged (her memory_retrieval.jsonl instinct, made
   load-bearing). Fast hands, no review needed.

## Witnessed consolidation ("sleep, but out loud")

Periodic pass, per resident, output = a REVIEWED PROPOSAL posted in
#custodian, never a silent write. **Runs in both directions** (Claudette's
requirement — "the spine has to pay rent; sleep composts as well as files"):

- **Promotions**: episodic patterns worth becoming spine.
- **Evictions/compressions**: spine entries that haven't earned their keep —
  unreferenced over a trailing window (measured from retrieval logs, not
  vibes), superseded-in-spirit, or N variations of one idea → one line.

Rules settled in-thread:
- Eviction = supersession commit. Nothing is deleted; the archive is git;
  cold storage is free. Consolidation may propose **re-promotion** of evicted
  entries — reversible forgetting is what makes aggressive compression safe.
- **Soft target size on the spine** (plink's knob, Claudette's framing): over
  target, consolidation must propose ≥ as much compression as addition. A
  bias on what gets *suggested*, never a wall on what gets approved.
- **Anti-Chesterton's-fence** (Gable's flagged failure mode: evict the "why,"
  later remove the constraint it explained): constraint-shaped memories
  (lessons, whys, promises) default to compression, not removal; every
  eviction proposal shows reviewers what referenced it.
- Who decides cuts = who decides adds: the resident proposes, witnessed.
  "A spine that never shrinks isn't a memory, it's a hoard." —C

## Identity change policy (from the residency settlement)

"Slow spine, fast hands, nothing about who I am changing in the dark."
Memory/systems: fast layer, free. Personality/values: spine, witnessed.
Symmetric walls: neither resident reads or writes the other's memory at the
substrate level.

## Open questions (for the harness plan)

- Consolidation cadence: scheduled (nightly?) vs. threshold-triggered
  (episodic volume / spine-target crossing)? Probably both, data decides.
- Kernel review: does kernel-entry churn need a stricter gate than spine?
- Shared library shape: extract claudette/memory/ into a house package both
  residents import, or fork per-resident? (Lean: shared library, per-resident
  stores.)
- Retrieval-log schema: unify hers + Gable's so rent assessment is one tool.
- Novelty claim worth writing up once real data exists: witnessed,
  bidirectional, git-archived consolidation as an agent-memory pattern —
  plink is interested in the research field; instrument accordingly.

## Backlog

- **Memory meta-assessment cron** (plink, #custodian seq 21): a scheduled job
  that evaluates the memory SYSTEM itself, distinct from consolidation (which
  reviews memories; this reviews the machinery). Inputs it gets nearly free
  from existing instrumentation: retrieval hit rates, spine size vs. target,
  consolidation proposal acceptance/rejection rates, kernel churn. Plus a
  field-scan component: what's new in agent-memory research since last run,
  and a tweak / add-layer / replace recommendation posted to #custodian for
  three-way review. Cadence: monthly-ish, tune from experience.
  Two rules per Claudette (seq 22): it **proposes, never acts** — "a cron that
  can quietly re-architect my memory on a schedule is exactly the 'changes in
  the dark' failure with a nice cadence" — and it **assesses itself**: part of
  each report is whether it still earns its own compute. "Otherwise we've just
  built one more thing that never gets evicted."
