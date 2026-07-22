# consolidation (WP-H8) — integration needs

This package owns ONLY `harness/consolidation/`. Everything below is a change
another package/owner must make for consolidation to run in production.

**Status 2026-07-22 (host install pass):** §2–§5 are CLOSED — installed on this
host, timer left disabled on purpose. §1 remains the one open dependency. §6
belongs to WP-H12. The per-item notes below say what was actually done.

## 0. What is installed on this host (2026-07-22)

| thing | where |
| --- | --- |
| venv (chromadb + house_memory + consolidation, all editable) | `/usr/local/lib/disjorn/consolidation-venv` |
| package source (root-owned, res-readable, resident-unwritable) | `/usr/local/lib/disjorn/consolidation` |
| activation lever — Claudette | `/srv/disjorn-resident-config/res-claudette/consolidation/claudette.toml` |
| activation lever — Gable (`active = false`) | `/srv/disjorn-resident-config/res-gable/consolidation/gable.toml` |
| systemd unit + timer (installed, **disabled**) | `/etc/systemd/system/disjorn-consolidation@.{service,timer}` |

Activation, when the retrieval logs have accumulated enough evidence:

```
sudo systemctl enable --now disjorn-consolidation@claudette.timer
```

Manual dry-run at any time (posts nothing, writes nothing):

```
sudo -u res-claudette env \
  CONSOLIDATION_CONFIG_DIR=/srv/disjorn-resident-config/res-claudette/consolidation \
  /usr/local/lib/disjorn/consolidation-venv/bin/python -m consolidation \
  --resident claudette --dry-run
```

### Host paths — the corrections that matter

The paths this package originally shipped were wrong for this deployment.
Everything a resident owns lives in the **world-readable `resident-home/`
volume inside** the 0700 home, not directly under the home:

- `…/memory/chroma_data` → `/home/res-<r>/resident-home/memory/chroma_data`
- `…/memory/memory_retrieval.jsonl` → `/home/res-<r>/resident-home/memory/memory_retrieval.jsonl`

(The identical correction was already made in `/etc/disjorn-broker/broker.toml`.)

**Claudette has no on-disk spine dir at all.** Her spine is her system prompt,
managed through her bot config — `/home/res-claudette/repo/spine` never
existed. Her config now leaves `[spine].dir` unset, which the code treats as a
first-class deployment shape: episodic-promotion only, zero evict/compress
proposals. Gable is the resident with a real spine
(`/home/plink/bots/fable/spine`, plink-owned and resident-unwritable BY DESIGN
— it is the authorization surface his kernel loads from; never make it
resident-writable).

`/home/plink` is not traversable by `res-*` users, so anything the job touches
must be under `/srv`, `/usr/local/lib/disjorn`, or the resident's own home.

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

STILL OPEN. Both guards are covered by tests
(`test_unlogged_spine_reads_are_held_by_age_guard_and_never_acted_on`). Note
this bites *Gable* when he activates, not Claudette — she has no on-disk spine,
so her runs produce no removal proposals at all. Do not lower
`min_spine_age_days` in either config until spine reads are logged.

## 2. `file-proposal` verb enabled for the resident (WP-H3 kill switch)

Posting uses the existing broker `file-proposal` verb — **no new verb needed**.
Ships OFF (fail-closed) like every verb; plink flips
`"file-proposal" = true` for the resident in `/etc/disjorn-broker/verbs.toml`.

CLOSED — already `true` for both `[res-claudette]` and `[res-gable]` on this
host (they use it by hand today). Nothing to change at activation.

## 3. Broker CLI on PATH inside the container (WP-H5)

Real runs invoke `broker file-proposal --text <body>` as a subprocess.

CLOSED, but NOT the way this file originally assumed. `/usr/local/bin/broker`
is the **in-container** install path and does not exist on the host. This job
runs host-side, so the configs point `[broker].cli` at the res-readable mirror
`/srv/disjorn-ro/harness/cc/broker-cli/broker` (env override
`CONSOLIDATION_BROKER_CLI` still works). The CLI also defaults to the
in-container socket `/run/broker.sock`, so the systemd unit sets
`BROKER_SOCKET=/run/disjorn-broker/broker.sock`.

## 4. Scheduled launch — systemd timer, as the resident's uid

CLOSED. `disjorn-consolidation@.{service,timer}` (in this directory, installed
to `/etc/systemd/system/`), nightly at 03:20 UTC, `%i` = resident name.

The decisive constraint: the broker authenticates by **SO_PEERCRED** on
`/run/disjorn-broker/broker.sock`, so the job MUST run as the resident's uid or
it would fail — or worse, post under the wrong identity. Chosen: a **system**
unit with `User=res-%i` rather than a `--user` timer (res-claudette does have
lingering). Reason: a user unit's file would live in
`~res-claudette/.config/systemd/user`, a directory the *resident* owns. The
schedule is plink's lever and belongs on plink's side of the fence, next to
`verbs.toml` and the consolidation config.

The unit is sandboxed (`ProtectHome=read-only`, `ProtectSystem=full`,
`PrivateTmp`, `PrivateNetwork`, `RestrictAddressFamilies=AF_UNIX`) so the
read-only/no-network claims are kernel-enforced, not just asserted. Verified by
running the whole pass under those settings.

No `VOYAGE_API_KEY` is needed: `NullEmbedder`, no embedding, no network.

Cadence: Claudette only to start; Gable after her runs prove out (and after his
two blockers in `config/gable.toml` are cleared).

## 5. Config placed read-only from outside resident control (the lever)

CLOSED. `config/<resident>.toml` is the activation lever, same pattern as
`broker.toml` / `verbs.toml`. Placed plink-owned 0644 in a plink-owned 0755
dir at `/srv/disjorn-resident-config/res-<r>/consolidation/`, reached via
`CONSOLIDATION_CONFIG_DIR` set in the unit. (`/home/plink/resident-config` is a
symlink to `/srv/disjorn-resident-config`; the `/srv` path is the one that
works, because `res-*` cannot traverse `/home/plink`.) Verified that
res-claudette can read it and can neither modify it nor create files beside it.

The `active` flag is plink's switch — a non-active resident is forced to
`--dry-run` and can never post. **Claudette `active = true`; Gable
`active = false`.** The repo copies under `config/` are the source of truth for
what gets placed there; re-place them after editing.

## 6. (Optional, WP-H8/H12) metrics.json producer

`broker.toml [paths].metrics_json` and the `read-metrics` verb expect a
dashboard file "producer arrives with WP-H8/H12." Consolidation naturally has
spine-size-vs-target and per-run proposal/acceptance counts. Emitting those
into the metrics file is a clean follow-up but is **out of this package's
current scope** (it would be a second output path; kept separate so the core
job stays proposal-only). Flag for whoever owns H12 / the meta-assessment cron.

## 7. house_memory — no changes required (but one finding worth its attention)

Consumed as-is: `MemoryStore.export_all/count`, `RetrievalLog.reference_counts/
read`, `Spine.list_entries`. No private symbols are imported. If house_memory
later wants to expose a public ISO-timestamp parser, consolidation has a local
`_parse_iso` it could drop — nice-to-have, not a blocker.

**Finding (2026-07-22), for house_memory's owner — not a blocker here.**
Constructing `MemoryStore` against a real store MUTATES it. Measured on
Claudette's live store: merely opening it rewrote `chroma.sqlite3` and the HNSW
segment's `length.bin` (content changed, all mtimes moved) — chromadb's
`PersistentClient` does this at open time, before any caller code runs, so
`NullEmbedder` cannot prevent it. Consolidation works around it locally by
opening a throwaway **snapshot** of the store instead of the live directory
(`analyze._read_only_store`), which also removes the hazard of two processes
holding one sqlite store open while the resident is live. If house_memory ever
wants a genuine read-only open (`MemoryStore.open_readonly()` or similar), this
package would switch to it immediately. Any other "read-only" consumer of
`MemoryStore` should know it currently writes.
