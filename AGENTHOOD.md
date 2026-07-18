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
