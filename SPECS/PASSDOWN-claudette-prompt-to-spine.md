# PASSDOWN — build Claudette's prompt→spine migration

Written 2026-07-23 at the end of a long session, for a fresh session to execute.
The spec `SPECS/2026-07-22-claudette-prompt-to-spine.md` is **confirmed** (plink,
keyboard) and **approved by Claudette as review owner** with TWO BINDING GATES.
This passdown is the build brief; the spec is the contract. Read both.

## The two binding gates (from Claudette — non-negotiable)

1. **BYTE-IDENTICAL acceptance test, first deliverable.** Prove the
   spine-recomposed prompt equals today's prompt **byte-for-byte** before ANY
   behavioural change lands. She insists because she cannot verify it herself —
   `bots/claudette/` is outside her read scope (the mirror doesn't carry it), so
   this test is her only guarantee no paragraph fell out the seam.

   **The exact target string** (from `bots/claudette/disjorn_bot.py:269`):
   ```
   core.SYSTEM_PROMPT + "\n\n" + PLATFORM_SUFFIX
   ```
   composed only when `DISJORN_PROMPT_SUFFIX` is truthy, else `system=None`.
   `SYSTEM_PROMPT` is a parenthesized string-concat literal at
   `bots/claudette/core.py:36`; `PLATFORM_SUFFIX` is one at
   `bots/claudette/disjorn_bot.py:42`. The recomposed-from-spine value must equal
   that concatenation exactly, and the `DISJORN_PROMPT_SUFFIX` toggle behaviour
   must be preserved (spine → full prompt when on; the suffix entry drops when
   off, or an equivalent that keeps `None`-when-off — decide and TEST it).

2. **Composition order = explicit frontmatter ordinals, FAIL CLOSED.** No
   `readdir`-order dependence. If any entry lacks its ordinal, OR the
   kernel/persona entry is absent, composition **aborts loud** — never emits a
   partial Claudette. Same discipline as the privacy wall.

## The shape of the work, and the parts that are NOT like Gable's

- **Her adapter is a SEPARATE git repo**, at
  `/home/res-claudette/resident-home/bots/claudette/` (not the Disjorn repo, not
  `/home/plink/bots/...`). Confirmed it is its own repo. So this build spans two
  repos: her adapter repo (the code change) and a new plink-owned spine dir +
  mirror (the content).
- **She is NOT a Claude Code resident.** Her container's main process is her
  Python adapter (`disjorn_bot.py`), a long-running `resident-cc.service` — NOT
  a per-summon `claude -p`. So `house_memory/bootstrap.py` is irrelevant to her;
  the composition happens in HER Python at startup/per-message, reading the
  spine dir and building the `system=` string. This is adapter code, not config.
- **Her adapter does NOT import `house_memory`.** So reusing the `Spine` class
  (which Gable's build extended with seats+ordinals, on branch
  `loop/2026-07-22-gable-spine-ro-cutover-seat-split`) means either (a) adding
  `house_memory` as a dependency into her container/venv, or (b) writing a small
  standalone composer in her adapter. **This is a real design fork — decide it
  deliberately and note it for review.** (a) keeps one spine implementation for
  the house (DRY, and her frontmatter-ordinal + fail-closed rules would be the
  same code that serves Gable); (b) avoids coupling her adapter to a Disjorn-repo
  package. Leaning (a) for a single source of truth, but it is not obvious.

## Her granularity (from the spec) — the entries to split into

Persona kernel (kernel-flagged, consolidation may never rent-evict) / physical
description / relationship context / tool-use discipline / platform-situational
(≈ today's `PLATFORM_SUFFIX`). Split the EXACT current text — byte-faithful, gate
1 — do not paraphrase. The platform-situational entry is the one gated by
`DISJORN_PROMPT_SUFFIX`.

## Build discipline

- Branch it (`loop/2026-07-22-claudette-prompt-to-spine` or per BUILD-LOOP).
  Do NOT merge, do NOT mutate her live running adapter, do NOT touch her live
  container. The result waits for her review of the diff — her rule, "the diff
  is the authorization."
- Spine content goes to a plink-owned dir mirrored RO the way Gable's is
  (`/srv/disjorn-spine/claudette`, published by `harness/keyboard/06-spine-mirror.sh`
  — that script currently hardcodes `gable`, may need a claudette arg/param;
  check). Her adapter reads the RO mount, never a writable copy — this is the
  KB-D10 close, same wall as Gable's.
- The byte-identical test is the acceptance gate: it must exist and pass in the
  build, and be runnable by plink at review. Make it the first commit.
- Fail-closed composition (gate 2) needs its own tests: missing ordinal aborts,
  missing persona/kernel entry aborts, and neither silently degrades.
- Tier 2, custodian lane, Claudette review owner. Comment densely.

## After the build

Claudette reviews the diff (she asked, at seq level this session, for the confirm
to ideally be "witnessed in #custodian" — the spec records that as her stated
preference, not yet satisfied; consider posting the confirm there). Then plink
applies: merge her adapter change in her repo, publish her spine mirror, restart
`resident-cc`. Nothing self-applies.

---

## Live state at end of this session (2026-07-23) — orientation for the next session

- **Gable**: live, on plink's **Max subscription** via `CLAUDE_CODE_OAUTH_TOKEN`
  (cutover done this session; a dropped-leading-`s` paste artifact caused a
  transient 401, fixed). Running Fable, RO spine mount at `/opt/spine`,
  `model_gate = "alert"`, `--output-format stream-json --verbose`, #custodian
  backfill reverted to **30** (KB-D14 stopgap — the deeper-100 window re-exposed
  the poisoned seq region; restore 100 only after KB-D15 ingest hygiene).
- **Gable's seat-split** is BUILT on branch
  `loop/2026-07-22-gable-spine-ro-cutover-seat-split` (verified: resident load
  byte-identical to today, build seat bakes operational-minus-biography, unknown
  seat fails loud). **Awaiting Gable's review** of the diff + the one design-fork
  question (build seat bakes everything it loads because a detached build has no
  retrieval loop). Apply steps in `SPECS/proposed/gable-spine/APPLY.md`. A
  worktree exists at `.claude/worktrees/agent-a6535609f37065695`.
- **Kill switches**: every action verb OFF except the read-only set +
  `refresh-mirror` (now genuinely wired and proven end-to-end; images refreshed
  into both resident stores — residents CANNOT build their own image, the egress
  wall blocks the registry; build-as-plink then `save`/`load`). `start-build`
  OFF for both.
- **model_gate**: shipped `off` by default in the template; Gable's live config
  is `alert`. The BL-G1 decision was `alert` for now (builds too), gather data
  before `refuse`.
- **Big open items**: KB-D15 (gate-as-oracle ingest hygiene — the real drift
  fix), WP-A1 (broker gets own uid; plink RULED: do A1, agent account gets
  NOPASSWD:ALL, keep plink-nopasswd — see AUTHORITY-PLAN.md), and this Claudette
  build. Red-team backlog (RED-TEAM-BACKLOG.md) runs in an isolated Opus venue,
  never in a channel residents backfill.
