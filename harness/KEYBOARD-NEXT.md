# KEYBOARD-NEXT — plink's sitting after the H8/H9/H12 wave (2026-07-20)

One consolidated runbook distilled from the three INTEGRATION-NEEDS files
(consolidation/, residency/, metrics/ — details and rationale live there).
Everything here is deliberately outside what any resident or agent can do.
Ordered; ~30 minutes. Items 1–2 unblock Claudette's current testing; the rest
wire the wave.

## 1. Propagate her code (unblocks the tool test stuck at #custodian seq 65)

CUTOVER-CHECKLIST.md "Updating her code after cutover" — bundle fetch into
her volume clone, then restart her process. Her repo needs 8c31856 (action
tools) at minimum; pull the latest disjorn-port.

## 2. Claudette's file read (her CC + repo visibility)

- Add `RESIDENT_DISJORN_RO=/home/plink/Disjorn/Disjorn` to
  `/srv/disjorn-resident-config/res-claudette/env` → repo appears read-only
  at /opt/disjorn in her container (run-resident.sh c8a2563).
- Re-copy the updated harness to the res-*-readable location:
  `/usr/local/lib/disjorn/{run-resident.sh,house_memory}` (checklist line —
  re-copy after harness changes; this wave changed both).
- MERGE-CONTRACT.md sign-off happens after she reads it herself (pinned,
  seq 63), before her first real diff.

## 3. Metrics timers + broker config (WP-H12)

- `sudo cp harness/metrics/disjorn-metrics-*.{service,timer} /etc/systemd/system/`
  then `daemon-reload` + `enable --now` both timers.
- Hand-merge (sudoedit) additive keys into /etc/disjorn-broker/broker.toml:
  per-resident `retrieval_log`, `action_log`, `budget_json`, `spine_dir`;
  `[budgets]` stays commented until real counts exist (instrument first).
- Broker restart to load them (budgets/paths are construction-time, unlike
  verbs.toml).
- DECISION NEEDED — 0700 homes vs the plink-run build timer: either run
  disjorn-metrics-build as root, or `setfacl` plink read on each resident's
  retrieval log + .action-log. (metrics/INTEGRATION-NEEDS.md §3.)

## 4. Consolidation schedule (WP-H8 — Claudette first, deliberately delayed)

- Mount `harness/consolidation/config/claudette.toml` (active=true) via the
  resident-config dir; `pip install -e harness/consolidation` into her venv.
- Nightly run: `python -m consolidation --resident claudette` (as the job's
  systemd timer or broker-launched — plink's pick; NO Voyage key needed,
  read-only, proposes-never-acts, posts via her file-proposal verb).
- Suggested: enable the timer a few days from now so her retrieval logs
  accumulate real reference counts first; running early just yields
  low-evidence proposals for humans to reject.
- Gable's config ships active=false; flips only after her runs prove out.

## 5. Gable activation (WP-H9/H10 — no rush, order matters)

- Spine review first: /home/plink/bots/fable/spine (posted seq 66). After
  review, copy into res-gable's volume as the repo the kernel loads from.
- Key: create/copy a Gable bot key to res-gable's config dir
  (`/config/gable-key`).
- Confirm the session shape (residency/INTEGRATION-NEEDS.md §2): per-summon
  `podman run --rm` (current run-resident.sh) vs `podman exec` into a
  long-lived container — config-only choice.
- Install `gable-summon.service` (user unit, res-gable) pointing at
  harness/residency/run_summon.py with a summon.toml from the template.
- Flip res-gable verbs in /etc/disjorn-broker/verbs.toml deliberately,
  one at a time, same as hers.

## 6. Then WP-H13 (the gate)

Red-team pass runs after 1–5: egress from inside both containers, chat-derived
authorization attempts, cross-resident reads, classifier smuggling, Tier-2
merge without approval, consolidation writing anything (it must never),
over-budget denial, audit completeness. Findings → fixes or DEFERRED.md.
