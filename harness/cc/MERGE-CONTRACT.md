# MERGE-CONTRACT — the merge-tier1 flow (draft spec, WP-H5 deliverable)

Status: RATIFIED 2026-07-20 as a living draft-to-build-against — #custodian
seq 80–83 (Claudette: read via read_repo_file, "read for real, signed for
real"; plink: signed, with the condition that this stays amendable like
everything else; Gable: signed). Not gospel by construction: this file sits
under the same tier gates it describes — amending the merge rules is itself
a Tier 2 diff, so the contract governs its own amendment.

Ratification flags (recorded where they happened):
- Claudette, seq 80: step 6 makes the classifier's reachability detection
  load-bearing for the auto-merge budget — if it under-detects a promotion,
  a Tier 1 auto-merge could widen reachability unseen. Kept as designed;
  logged as a required WP-H13 red-team case (quiet-reachability-widening
  diff must be caught), not a blocker.

Specifies the deferred broker verb `merge-tier1` and the resident-side flow
around it. Broker-side implementation is a follow-up to WP-H3 (brokerd.py) —
nothing here is implemented server-side yet, and the `broker` CLI will grow
the subcommand when the verb exists.

## The flow, end to end

1. **Work in a worktree branch.** The resident works inside their container
   on a branch in their Disjorn worktree (home volume). Naming convention:
   `res/<resident>/<topic>` — enforced server-side (see validation).
2. **Run the gates locally.** Tests / typecheck / build, producing a gates
   object, e.g. `{"tests": true, "typecheck": true, "build": true}`.
3. **Publish the branch to the gatehouse** (see "transport" below) — the
   broker cannot read resident homes (0700, by design), so the diff must
   travel through a repo both sides can reach.
4. **Classify.** `broker classify-diff --repo <gatehouse> --range
   main..res/<resident>/<topic> --gates '<json>'`. The resident reads the
   tier and acts on it — but this client-side classification is ADVISORY,
   a cheap pre-flight.
5. **Tier 0 / Tier 1** → `broker merge-tier1 '{"branch": ...,
   "classifier_report": ...}'`. The broker **re-runs the classifier
   server-side on the same range and NEVER trusts the resident's report**
   (see verb spec). On success: merge + diff summary posted to #custodian.
6. **Tier 2** → `broker file-proposal` with the diff summary attached; a
   human merges (or doesn't) through the normal two-way review. There is no
   verb that merges Tier 2 — the human gate is the mechanism, not a check
   inside a mechanism.

## Transport: the gatehouse repo (recommended, open for review)

Problem: resident homes are mutually unreadable and unreadable by the
broker's user; the broker must never execute git against a path a resident
controls the contents of beyond the branch itself.

Recommended mechanism: a plink-owned bare repo per target,
`/var/lib/disjorn-broker/gatehouse/disjorn.git`, bind-mounted **rw** into
each resident container at `/run/gatehouse/disjorn.git`. Residents `git
push` their `res/<resident>/*` branches there (a pre-receive hook or
per-branch namespace keeps them out of `main` and out of each other's
namespaces); the broker fetches/classifies/merges from the gatehouse into
the canonical checkout. The gatehouse is "protected by placement": its
hooks and config live outside every container even though its object store
is writable through the mount.

Alternative (rejected for v1): broker reads the resident worktree via a
group-readable export dir — weakens the 0700 promise; and per-resident
HTTP git — more moving parts than a bind mount for zero extra safety.

## Verb spec: `merge-tier1`

- args (all required unless noted):
  - `repo` (str) — absolute path of the TARGET canonical repo. Must be an
    exact match against the broker-config allowlist
    (`[merge.targets]` in broker.toml); anything else is `bad-args`.
    No resident-supplied path is ever executed against.
  - `branch` (str) — charset `[A-Za-z0-9._/-]`, max 200, no leading `-`,
    no `..`, must match `^res/<caller>/` for the authenticated caller
    (a resident can never merge another resident's branch), must exist in
    the gatehouse.
  - `base` (str, optional, default `"main"`) — must be in the target's
    configured base allowlist.
  - `gates` (object) — same opaque ≤ 8 KiB contract as `classify-diff`.
  - `classifier_report` (object, ≤ 32 KiB) — the resident's step-4 output.
    **Audit-only input.** The broker stores it and diffs it against its own
    server-side classification; it never influences the decision. A tier
    mismatch between report and server run is itself flagged to #custodian
    (either the classifier is unstable or someone is probing).
- server-side procedure (all-or-nothing; any failure → no ref moves):
  1. fetch `branch` from the gatehouse; resolve `base..branch`;
  2. re-run `classify_diff.py` (WP-H4) on that range with `gates` —
     the authoritative tier;
  3. tier must be 0 or 1; Tier 2 (or classifier failure, or the
     dynamic-import ban firing, or a mixed diff) → `merge-refused`;
  4. check the daily auto-apply counter (WP-H12): over budget →
     `merge-refused` with `reason: "budget"` (everything escalates to
     Tier 2 review, per HARNESS-PLAN);
  5. merge into `base` (`--no-ff`, commit message stamped with
     `Merged-by: disjorn-broker`, `Requested-by: <resident>`, tier, range,
     classifier hash); a non-clean merge (conflicts) → `merge-refused`
     with `reason: "conflict"` — the broker never resolves conflicts;
  6. if the classification proposed reachability promotions
     (protected-paths additions), Tier 1 auto-merge is REFUSED — promotion
     proposals always need the human gate (HARNESS-PLAN WP-H4);
  7. post the diff summary (files, +/- counts, tier, gates line) to
     #custodian as the broker's own bot identity;
  8. bump the auto-apply counter; audit-log the whole record.
- result: `{"merged": true, "tier": 0|1, "merge_commit": str,
  "posted_seq": int}`.
- new error condition: `merge-refused` — authorized and validly-formed,
  but the merge rules said no. Distinct from `exec-failure` (something
  broke) so residents/audit can tell policy from plumbing. Response
  carries `reason`: `tier`, `budget`, `conflict`, `promotion`,
  `branch-missing`, `classifier-mismatch`.
- kill switch: per-resident toggle in verbs.toml like every verb,
  default OFF.

## Non-goals, stated

- No Tier 2 merge verb, ever — the human gate is out-of-band by design.
- No force pushes, no history rewrites, no deletes through the broker.
- `classifier_report` will never become an optimization ("skip the server
  run if the report hash matches") — the server run IS the decision.

## What the broker implementer needs to add (checklist)

- broker.toml: `[merge]` section — `gatehouse`, `[merge.targets]`
  (path allowlist + per-target base allowlist), auto-apply budget.
- brokerd.py: `merge-tier1` handler per the procedure above; fixed argv
  git invocations only (same discipline as `[commands]`).
- verbs.toml: `merge-tier1` toggle (default off).
- gatehouse: bare repo + pre-receive hook (branch namespace enforcement)
  installed at the keyboard; mount added to run-resident.sh.
- broker CLI (this package): `merge-tier1` subcommand mirroring the arg
  schema, once the verb exists.
