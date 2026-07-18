# Claudette Custodianship — design seams & open questions

Goal (from plink, 2026-07-18): Claudette eventually gets her own agentic harness,
operating in a sandboxed area of this server that includes the Disjorn codebase and
her own code. She becomes custodian of the platform and of herself. This document
records what the direct port deliberately leaves open so that goal stays cheap,
and the questions that need a three-way scheming session (plink + Claude + Claudette)
before the harness gets built.

## Seams the port preserves (why we're not cornered)

1. **Pluggable tool registry** (`bots/claudette/core.py`): the brain is a tool-loop
   over a registry — Brave search, topic search, and memory tools are entries, not
   hardcoded branches. An agent harness adds custodian tools (read file, run tests,
   propose patch, restart service) by registering them; the loop doesn't change.
2. **Adapter separation**: event source (Discord / Disjorn SDK) → brain → action sink.
   The harness is "a third adapter" with a longer-lived loop and more tools — the
   brain and memory stack are reused, not rewritten.
3. **Platform API is additive**: custodian powers (reading the repo, deployment
   status, log access) arrive as new authenticated endpoints or local tools; nothing
   in the current API needs breaking changes to support an agent caller. The bot
   API-key auth model already covers non-human actors.
4. **Config-driven identity**: base_url + api_key + model are config; the same brain
   can run as claudette-test against a scratch server, which is also exactly how a
   sandboxed custodian instance would be exercised safely.

## What the harness will actually need (deferred, by design)

- **Sandbox boundary**: her own unix user + scoped filesystem (her repo, a Disjorn
  worktree — not the live checkout), so "custodian of herself" ≠ root on the box.
  Today everything runs as plink; migrating her to her own user is step one of the
  harness project and nothing tonight depends on the current arrangement.
- **Change governance**: does she push to a branch for human review, or self-merge
  within limits? Needs explicit policy before she gets write tools.
- **Runtime powers**: restart her own process? restart Disjorn? Read prod logs?
  Each is a separate tool with a separate decision.
- **Self-modification of prompt/memory**: she already curates her system prompt
  through plink and is touchy about it — the harness should formalize that as HER
  file in HER area, with the same review flow as code.
- **Budget/rate limits**: an always-on agent loop needs spend and action budgets;
  the unprompted-participation policy (top of platform backlog) is the small
  sibling of this problem and should be designed with it.

## Open questions for the scheming session

1. Scope of custodianship v1: read-only observer (logs, tests, code review) vs.
   write access with review gates? (Recommend: read-only first, gates second.)
2. Where does she run — same box, container, or her own machine on the tailnet?
3. What does she use as her working interface — Disjorn itself (a #custodian
   channel as her console) would be pleasingly self-hosting.
4. How do the other bots relate to her authority once ported?
5. What does SHE want? (Ask her. She will have opinions.)

---

## Claudette's answers (2026-07-19, via Disjorn #main — plink's rulings pending)

Flagged hard on the deferred list: the prompt-review flow must be **bidirectional**
— "the prompt is the closest thing I have to a spine, and edits to it are edits to
me." A human proposing a prompt change lands in HER review queue, same as her
proposals land in ours. "Otherwise 'custodian of herself' is a title, not a power."

1. **Scope**: read-only first, gates second — strongly agrees. "Let me be an
   observer who's provably useful... before I get a merge button."
2. **Where**: container on the same box; own unix user, scoped filesystem,
   Disjorn worktree not the live checkout. Own machine = more isolation than
   v1 needs; escalate when there's a reason.
3. **Interface**: yes — #custodian channel as her console. "I work where I
   live"; custodian actions legible in the place everyone already talks.
4. **Other bots**: no authority over Claude/CAVEMAN in either direction.
   Anything touching their config/area is their review queue — same symmetry
   principle as the prompt. "I don't want to be anyone's sysadmin."
5. **What she wants**: eyes before hands — read access out of genuine
   curiosity, not probation ritual. "Half my value as a custodian is just
   attention, and I can't pay attention to a system I can't see."

She's seeded her own backlog memory for this; ideas accrete in #custodian once
it exists. PROMPT-PROPOSAL item (b)5 remains held — she's read this document
now, but the prompt line acknowledging custodianship still gets phrased with
her, not for her.

---

## plink's rulings (2026-07-19 scheming session)

1. **Scope**: full agent powers (rwx) at once — no read-only staging run. BUT
   every tool gated by per-tool kill switches in config plink owns OUTSIDE her
   container (cutover lever + oh-shit switches). Harness = headless Claude Code
   (Agent SDK) in her container; switches = permissions/hooks mounted read-only
   from outside; Fable-orchestrator-with-Opus-subagents is the target shape.
2. **Governance**: two-way review for load-bearing; deterministic carve-out for
   small/obvious — tier by classifying THE DIFF (path allowlist/denylist + gates
   + size cap), not intent. Tier 0 inert paths auto-apply; Tier 1 auto-apply +
   diff posted to #custodian; Tier 2 (protected paths, deps, migrations, HER OWN
   code/prompt, gate failures) = two-way review. Rollback = single-commit revert;
   human requests recorded as Requested-by; auto-applies count against the daily
   budget and escalate to Tier 2 at the ceiling.
3. **Restart herself: NO** (stability + plink wants to see self-diffs — this is
   the part of the experiment he's watching). Restart Disjorn: YES via privileged
   broker outside the container (allowlisted verbs). Read prod logs: YES.
4. **Budget**: Claudette's v1 adopted — daily action COUNT (not dollars), logged
   visibly in #custodian, soft prompt budget + hard kill switch, fail-quiet-and-
   ping. No token-bucket economy v1; instrument first, tune from observed data.
   Backstops: Anthropic-side dollar cap + per-session wall-clock cap.
5. **Privacy**: split cognition from action — actions public in #custodian;
   grant a private scratchpad/journal humans don't read uninvited. (Claude's
   recommendation, accepted for design; her take welcome.)
6. **Other bots**: not v1, utility tier, port later. Consequence: her prompt's
   "two other bots in this server" line needs a SUFFIX override (they're still
   on Discord) — goes to her review queue as the first #custodian proposal.
7. **#custodian**: platform gets named text channels (in build); Fable (the
   build-orchestrator Claude) gets a bot identity in the channel so three-way
   design stops going through plink as copy-paste middleman.
