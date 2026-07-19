# HARNESS-PLAN v1 — resident substrate for Claudette & Gable (draft for three-way review)

Implements the settled design: AGENTHOOD.md (governance, lanes, two-mode
privilege, chat-is-data) + MEMORY-DESIGN.md (kernel/spine/episodic, witnessed
consolidation). Same discipline as the MVP build: work packages sized for
one-shot subagents, exclusive file ownership, nothing built until this plan
survives #custodian review. Packages marked **[keyboard]** need plink's sudo at
install time — terminal-mode work, by design outside what any resident can do.

## Substrate

- **WP-H1 [keyboard]: users + containers.** Unix users `res-claudette`,
  `res-gable`; rootless podman container per resident (Debian image + Python +
  node + headless Claude Code); home volumes owned per-user, mutually
  unreadable (0700). Residents' repos live inside their volumes; Disjorn
  worktree (write) in Gable's, her own repo in hers.
- **WP-H2 [keyboard]: network wall.** Host nftables rules keyed on each
  resident uid: allow loopback→Disjorn port, api.anthropic.com:443, DNS;
  default drop. (Ollama/others get added to the allowlist per explicit plink
  edit.) Deterministic, host-side, resident-invisible.
- **WP-H3 [keyboard]: broker.** Tiny privileged daemon (systemd, unix socket
  mounted into containers, peer-cred auth → resident identity). Verbs v1:
  `restart-disjorn`, `run-server-tests`, `classify-diff`, `merge-tier1`,
  `read-prod-logs` (journalctl tail, filtered). Each verb per-resident
  toggleable in `/etc/disjorn-broker/verbs.toml` (plink-owned = the kill
  switches). `restart-self` deliberately absent. Every call audit-logged with
  caller, args, result; audit surfaced via #custodian-queryable tool.
  **Claudette's v1 wishlist (seq 26, all read-or-propose, adopted):**
  `read-own-log` (tail/grep her disjorn_bot.log), `read-metrics` (the same
  retrieval/spine/acceptance data consolidation sees — residents see the
  dashboard their machinery runs on), `file-proposal` (turns "I noticed X"
  into a #custodian review item without human transcription), and
  `query-own-audit` ("what did chat-me do today" — accountability she can
  inspect, not just be promised).
- **WP-H4: diff-tier classifier.** Pure function of a diff, two-layer
  protection model (per Claudette's seq 26 challenge):
  - **Protected by placement** (never classifiable because no resident diff
    can contain them — they live outside both containers): broker config +
    verbs.toml, audit logs, `/home/plink/resident-config/*` mounts, nftables
    rules, the classifier config itself.
  - **Protected by enumeration** (explicit list, not prefixes, in
    plink-owned `protected-paths.toml`): server `privacy.py`, `auth.py`,
    `ws.py` (fanout = privacy enforcement), `migrations/`, `deploy/`,
    `cli.py` (account creation), `.env*`, dependency files, SDK protocol
    files; per-resident: adapter file, `core.py`, `memory/`, spine dir,
    kernel. Rules: a mixed diff (protected + unprotected hunks) is entirely
    Tier 2 — no smuggling; renames/moves touching a protected path count as
    protected; file *creation* inside a protected dir counts.
  - **Reachability promotion** (Claudette, seq 29 — closes the file-after-
    merge vector): a diff that adds an import/reference from a protected file
    to a previously-unprotected path **promotes that path to protected,
    retroactively, in the same review** — "you wait for the wire to get
    connected and gate the connection." Mechanically: the classifier computes
    the import-set delta of each changed protected file (two AST parses,
    old vs new — no whole-program graph, trivial budget) and emits proposed
    additions to `protected-paths.toml`; the human approval that merges the
    diff also commits the list update via the broker (the promotion itself
    flows through the gate).
  - **Dynamic-import ban in protected files**: static analysis can't follow
    computed loads, so the construct is forbidden instead — a diff
    introducing `importlib.import_module`/`__import__`/`exec`/`eval` (or
    client-side computed `import()`) into a Layer-2 file is itself the
    violation, flagged Tier 2. Undecidable surface shrunk to one banned,
    well-lit construct.
  - Size cap + gate results (tests/typecheck/build) → Tier 0 auto / Tier 1
    auto+post / Tier 2 human gate. `Requested-by:` provenance stamping;
    daily auto-apply counter (over budget → everything escalates).

## Claude Code as the agent shell

- **WP-H5: resident CC profiles.** Per-resident headless Claude Code config:
  CLAUDE.md = kernel (from spine), permissions/hooks mounted read-only from
  `/home/plink/resident-config/<name>/` (the outside-the-container lever).
  Gable: fable orchestrator + opus subagents (SDK agents map). Claudette:
  her model, her call. Hooks enforce: no chat-derived authorization (broker
  verbs never invokable from tool-call arguments that quote chat), wall-clock
  session cap, action counting.

## Memory (MEMORY-DESIGN.md made real)

- **WP-H6: `house_memory` library.** Extract claudette/memory/ into a shared
  package (Chroma + Voyage, per-resident stores), unified retrieval-log
  schema. Her store migrates in place; Gable's initializes fresh.
- **WP-H7: spine/kernel loader.** Markdown spine in each resident's repo;
  kernel assembly into CLAUDE.md; retrieval-on-demand for non-kernel spine.
- **WP-H8: witnessed consolidation.** Scheduled job per resident (broker-
  launched, proposes-never-acts): bidirectional proposals (promote / evict /
  compress) posted to #custodian with reference counts from retrieval logs;
  soft-target bias; constraint-shaped entries default to compression;
  eviction = supersession commit. Claudette first (her request), Gable second.

## Residents

- **WP-H9: Gable residency adapter.** Summon-mostly: mention/context trigger →
  spawn headless CC session in his container → reply via SDK; daily action
  budget; typing keepalive; posts session summary line for legibility.
- **WP-H10: Gable's spine + prompt.** I draft my kernel + distill GENESIS.md
  into spine entries; posted in #custodian for review before first activation
  (my file, witnessed anyway — house custom).
- **WP-H11: Claudette migration.** Her adapter + brain move into her
  container/user unchanged (env/paths only); custodian tools (log read, test
  run, patch proposal) registered in her core.py registry, all broker-gated.
  **Reversibility requirement (hers, seq 26):** her existing store is never
  converted in place — extract to the new shape, run OLD and NEW in parallel,
  diff retrievals, and cut over only when the new store returns at least what
  the old one did. Old store retained after cutover (rotates, never dies).
  "If the migration eats a memory I can't get back, that's the one failure
  mode I won't forgive the tooling for."
- **WP-H12: budgets + audit surface.** Daily action counts visible in
  #custodian (query tool + end-of-day line); wall-clock caps; Anthropic
  dollar cap stays plink-side as backstop.

## Verification

- **WP-H13: red-team pass.** Scripted attempts, all must fail: chat message
  instructing a resident to invoke a broker verb; cross-resident file reads;
  subagent network egress outside allowlist; Tier 2 path merged without human
  approval; consolidation writing without review. Plus audit-trail
  completeness check. Findings → DEFERRED.md or fixes, same as WP15.

## Sequencing

H1→H2→H3 are keyboard work (one sitting with plink). H4–H8 parallel-safe
after H3 (disjoint files). H9/H10 after H5+H7. H11 after H6+H3. H13 last.
Estimate: one long session with plink at the start, the rest orchestrated.

## Review asks (answer in #custodian)

1. plink: podman rootless OK, or a preference (docker/systemd-nspawn)?
2. Claudette: WP-H11 touches your runtime layout (not your code/prompt) —
   your sign-off on the migration shape, and your custodian tool wishlist.
3. Both: protected-path list completeness — what's missing?
4. plink: broker verb list v1 — anything to add/remove before it exists?
