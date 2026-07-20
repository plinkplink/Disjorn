# consolidation — witnessed memory consolidation (WP-H8)

"Sleep, but out loud." A scheduled, per-resident pass that reads the fast
(episodic) layer, the retrieval log, and the markdown spine, and emits
**reviewed proposals** to #custodian — never a silent write. Implements the
witnessed-consolidation section of MEMORY-DESIGN.md, under the governance of
AGENTHOOD.md (proposes-never-acts, chat-is-data, witnessed changes).

## What it does

Three inputs, all READ-ONLY:

1. episodic store — `house_memory.MemoryStore`, read via `export_all()` (no embed)
2. retrieval log — `house_memory.RetrievalLog`, for reference counts (the rent)
3. markdown spine — `house_memory.Spine` (read side)

One output: a `ConsolidationReport` — a batch of bidirectional proposals:

- **promote** (episodic → spine): a pattern retrieved `>= promote_min_references`
  times in the trailing window has earned spine tenancy.
- **evict** (spine → *supersession commit*): a non-constraint spine entry
  unreferenced over the window hasn't paid rent. Expressed as a git
  supersession (move to cold storage), **never a deletion** — reversible
  forgetting is what makes aggressive compression safe.
- **compress** (spine → tighter): under-referenced but **constraint-shaped**
  entries (lessons / whys / promises) default to compression, never eviction
  (anti-Chesterton's-fence). N variations sharing a `topic` merge to one line.

Every proposal carries **reference-count evidence** from the retrieval logs
("measured from logs, not vibes").

### Soft-target bias

`soft_target_spine_size` is plink's knob. When the spine is over target,
consolidation proposes `>= as much reduction (evict + compress) as addition
(promote)` — it holds the weakest-evidence promotions back so `promotions <=
reductions`. This is a **bias on what gets suggested, never a wall on what may
be approved**; the report shows reviewers exactly how many were deferred.

## Proposes-never-acts

The job never mutates a store, a spine file, or a retrieval log. Its store is
built with `NullEmbedder`, which raises on any embed — so "makes no network
calls / never writes" is a tripwire, not just a claim. Its only side effect is
*posting proposal text* via the broker's `file-proposal` verb (the broker, not
the resident, holds the authority to post to #custodian).

## CLI

```
python -m consolidation --resident <name> [--dry-run] [--config-dir DIR] [--now ISO]
```

- `--dry-run` prints the full "sleep, out loud" report to stdout and posts
  nothing. **The only mode usable in the build worktree.**
- A real run posts one `file-proposal` per proposal (each independently
  reviewable in #custodian). It requires the resident's config to be `active`;
  a non-active resident is forced to dry-run.

Launch it on a schedule (broker or systemd timer) — see INTEGRATION-NEEDS.md.

## Config

Per-resident TOML in `config/<name>.toml`, overridable via
`CONSOLIDATION_CONFIG_DIR`. In production these are mounted read-only from
outside the container — the `active` flag is plink's lever.

- **`config/claudette.toml`** — `active = true` (first to activate; her request).
- **`config/gable.toml`** — `active = false` (second client; switch on later).

Knobs: `soft_target_spine_size`, `window_days`, `promote_min_references`,
`evict_max_references`, `min_spine_age_days` (0 disables the age guard),
`exclude_kernel`, `max_promotions`, `constraint_tags`, `constraint_keywords`,
`[broker].cli`.

## Tests

```
server/.venv/bin/python -m pytest harness/consolidation/tests -q
```

Synthetic stores (StubEmbedder), hand-written retrieval logs and spine files,
tmp dirs, a fake broker CLI double. No network, no Voyage, no real stores, no
broker socket. Covers promote/evict/compress generation, reference-count
evidence, soft-target bias, constraint→compression default, supersession
framing, dry-run, CLI, posting, and the no-mutation guarantee.

## Files

- `consolidation/config.py` — per-resident config load (`ConsolidationConfig`).
- `consolidation/embedders.py` — `NullEmbedder` (the read-only tripwire).
- `consolidation/model.py` — `Proposal` / `Evidence` / `ConsolidationReport`
  and their #custodian rendering.
- `consolidation/analyze.py` — the pass: `build_proposals(cfg, ...)`.
- `consolidation/poster.py` — dry-run print / broker-CLI posting.
- `consolidation/__main__.py` — the scheduled CLI entrypoint.
- `config/*.toml` — per-resident config (the activation levers).
- `INTEGRATION-NEEDS.md` — what other packages must do to run this in prod.
