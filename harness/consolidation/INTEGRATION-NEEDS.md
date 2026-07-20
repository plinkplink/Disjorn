# consolidation (WP-H8) — integration needs

This package owns ONLY `harness/consolidation/`. Everything below is a change
another package/owner must make for consolidation to run in production. Nothing
here is done by this package; the orchestrator integrates after this wave.

## 1. Spine retrieval must log spine-entry names into the unified log (WP-H7)

**The load-bearing one.** `reference_counts()` counts strings in the retrieval
log's `returned_ids`. Consolidation looks up:
- **episodic memories** by their uuid (already logged by `MemoryStore.recall`),
- **spine entries** by their frontmatter `name`.

So WP-H7's spine retrieval-on-demand MUST, whenever it serves a non-kernel
spine entry into a resident's context, append a retrieval-log line whose
`returned_ids` contains that entry's `name` (same `RetrievalLog`, same file).

Until this exists, EVERY spine entry reads as unreferenced → every eligible
entry becomes an evict/compress candidate. Two things keep that safe in the
meantime: the `min_spine_age_days` guard (young entries are never judged), and
— decisively — proposes-never-acts: a human reviews every proposal in
#custodian. But rent assessment is only meaningful once spine reads are logged.

## 2. `file-proposal` verb enabled for the resident (WP-H3 kill switch)

Posting uses the existing broker `file-proposal` verb — **no new verb needed**.
At activation, plink must flip `"file-proposal" = true` for `[res-claudette]`
in `/etc/disjorn-broker/verbs.toml`. Ships OFF (fail-closed) like every verb.

## 3. Broker CLI on PATH inside the container (WP-H5)

Real runs invoke `broker file-proposal --text <body>` as a subprocess. The
resident broker CLI (`harness/cc/broker-cli/broker`) must be installed at
`/usr/local/bin/broker` (its WP-H5 install location). Override for other
locations / tests: env `CONSOLIDATION_BROKER_CLI`.

## 4. Scheduled launch (broker-launched or systemd timer)

Run per resident on a cadence (MEMORY-DESIGN leaves nightly-vs-threshold open;
"both, data decides"). Command:

```
python -m consolidation --resident claudette
```

Requirements for the launcher:
- the `consolidation` package importable (PYTHONPATH=`harness/consolidation`,
  or `pip install -e harness/consolidation` into the resident venv — it depends
  on `house_memory`, already installed);
- read access to the resident's episodic store dir, retrieval log, and spine
  dir (all read-only — the job never writes them);
- **no** `VOYAGE_API_KEY` needed: consolidation uses `NullEmbedder` and never
  embeds (no network).

Suggested first cadence: nightly for Claudette only; add Gable after her runs
have proven out.

## 5. Config mounted read-only from outside the container (the lever)

`config/<resident>.toml` is the activation lever, same pattern as `broker.toml`
/ `verbs.toml`: mount it read-only from `/home/plink/resident-config/...` (or
wherever the H5 mounts live) and point the job at it with
`CONSOLIDATION_CONFIG_DIR`. The `active` flag is plink's switch — a non-active
resident is forced to `--dry-run` and can never post. **Claudette ships
`active = true` (first to activate); Gable ships `active = false`.**

## 6. (Optional, WP-H8/H12) metrics.json producer

`broker.toml [paths].metrics_json` and the `read-metrics` verb expect a
dashboard file "producer arrives with WP-H8/H12." Consolidation naturally has
spine-size-vs-target and per-run proposal/acceptance counts. Emitting those
into the metrics file is a clean follow-up but is **out of this package's
current scope** (it would be a second output path; kept separate so the core
job stays proposal-only). Flag for whoever owns H12 / the meta-assessment cron.

## 7. house_memory — no changes required

Consumed as-is: `MemoryStore.export_all/count`, `RetrievalLog.reference_counts/
read`, `Spine.list_entries`. No private symbols are imported. If house_memory
later wants to expose a public ISO-timestamp parser, consolidation has a local
`_parse_iso` it could drop — nice-to-have, not a blocker.
