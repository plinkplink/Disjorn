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
3. markdown spine — `house_memory.Spine` (read side) — **optional**: a resident
   may have no on-disk spine at all (see "Absent inputs")

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

The job never mutates a store, a spine file, or a retrieval log. Its only side
effect is *posting proposal text* via the broker's `file-proposal` verb (the
broker, not the resident, holds the authority to post to #custodian).

Three mechanisms, not one promise:

1. `NullEmbedder` raises on any embed — "makes no network calls" is a tripwire.
2. The episodic store is opened against a **throwaway snapshot**, never the
   resident's live chroma dir. chromadb's `PersistentClient` rewrites
   `chroma.sqlite3` and the HNSW segment files *just by opening a store*,
   before any of our code runs — measured on the live host, so this is not
   theoretical. Reading a copy is the only way "never writes her memory" is
   true. (It also removes the two-processes-one-sqlite hazard while she's
   live.) `test_live_episodic_dir_is_not_even_touched` is the tripwire.
3. The deployed systemd unit runs under `ProtectHome=read-only`,
   `ProtectSystem=full`, `PrivateNetwork=yes`,
   `RestrictAddressFamilies=AF_UNIX` — the kernel enforces it too.

## Absent inputs

Two situations that look alike and are deliberately handled differently:

- **No spine at all** (`[spine].dir` unset) — a real deployment shape:
  Claudette's spine is her system prompt, managed through her bot config, not a
  directory of markdown entries. The run does the episodic-promotion half and
  emits ZERO evict/compress proposals, said plainly in the report header. This
  is an explicit short-circuit, so "no spine dir" can never degrade into "empty
  spine, therefore evict everything".
- **A configured path that doesn't exist** — a stale config (the bug this
  package shipped with). `MissingInputError`, exit 3, nothing produced. That
  covers a missing spine dir *and* a missing episodic data_dir — the latter
  because `get_or_create_collection` would otherwise CREATE an empty collection
  in the resident's memory, i.e. a write.

## CLI

```
python -m consolidation --resident <name> [--dry-run] [--config-dir DIR] [--now ISO]
```

- `--dry-run` prints the full "sleep, out loud" report to stdout and posts
  nothing. **The only mode usable in the build worktree.**
- A real run posts one `file-proposal` per proposal (each independently
  reviewable in #custodian). It requires the resident's config to be `active`;
  a non-active resident is forced to dry-run.

## Deployment

Runs as a **systemd system timer with `User=res-<name>`**, host-side — not in a
container, not as plink. The broker authenticates by SO_PEERCRED on its unix
socket, so the job must carry the resident's own uid; and the unit file belongs
in root-owned `/etc/systemd/system` rather than a resident-owned
`~/.config/systemd/user`, because the schedule is plink's lever.

See INTEGRATION-NEEDS.md §0 for the installed paths, the manual dry-run
command, and the single activation command.

## Config

Per-resident TOML in `config/<name>.toml`, overridable via
`CONSOLIDATION_CONFIG_DIR`. In production these are placed read-only from
outside resident control (`/srv/disjorn-resident-config/res-<r>/consolidation/`)
— the `active` flag is plink's lever, and the repo copies here are the source
of truth for what gets placed there.

- **`config/claudette.toml`** — `active = true` (first to activate; her request).
  No `[spine].dir`: she has no on-disk spine.
- **`config/gable.toml`** — `active = false` (second client; switch on later).
  Has a real spine dir; see the blockers noted in the file.

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
framing, dry-run, CLI, posting, the no-mutation guarantee (including that the
live episodic dir is byte-identical after a run), the absent-spine and
stale-path behaviours, and that the shipped configs use the real host paths.

## Files

- `consolidation/config.py` — per-resident config load (`ConsolidationConfig`).
- `consolidation/embedders.py` — `NullEmbedder` (the read-only tripwire).
- `consolidation/model.py` — `Proposal` / `Evidence` / `ConsolidationReport`
  and their #custodian rendering.
- `consolidation/analyze.py` — the pass: `build_proposals(cfg, ...)`.
- `consolidation/poster.py` — dry-run print / broker-CLI posting.
- `consolidation/__main__.py` — the scheduled CLI entrypoint.
- `config/*.toml` — per-resident config (the activation levers).
- `disjorn-consolidation@.service` / `.timer` — the deployed schedule
  (templates; installed to `/etc/systemd/system/`, `%i` = resident name).
- `INTEGRATION-NEEDS.md` — deployment state + what other packages still owe.
