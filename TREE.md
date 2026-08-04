# The tree — which copy is real, and whose filesystem it is on

Read this before quoting a path. Three afternoons have been lost to the same
mistake, and it is never the topology — it is that **container paths and host
paths are typographically identical**, so a path with no "whose filesystem"
sticker is ambiguous exactly when it matters.

## The one rule

**There is ONE writable copy of the repo.** Everything else is a read-only
derivative that syncs one way, or a resident's own volume you cannot write.

```
        /home/plink/Disjorn/Disjorn          ← YOU EDIT HERE. Only place a commit means anything.
                    │
      ┌─────────────┼─────────────┬──────────────────────────┐
      ▼             ▼             ▼                          ▼
  GitHub        /srv/disjorn-ro   /usr/local/lib/disjorn   server runs FROM the repo
  (off-box      (residents read   (DEPLOYED code: the       (restart to pick up
   mirror)       this as          copy that actually        server python)
                 /opt/disjorn)     RUNS for residents)
```

## Where a given file actually runs from

| What | Edited at | Runs from | To deploy |
|---|---|---|---|
| Server python | `server/app/…` | **the repo** | `sudo systemctl restart disjorn` |
| Client PWA | `client/src/…` | `client/dist` | `cd client && npm run build` |
| `house_memory` | `harness/house_memory/…` | **`/usr/local/lib/disjorn/house_memory`** | copy, then restart the resident |
| consolidation | `harness/consolidation/…` | `/usr/local/lib/disjorn/consolidation` | copy (walker is OFF anyway) |
| Claudette's code | `/home/plink/bots/claudette` (branch `disjorn-port`) | her volume clone | `./claudette-update.sh` |
| Gable's spine | `/home/plink/bots/fable/spine` | `/srv/disjorn-spine/gable` → `/opt/spine` `:ro` | publish; next summon picks it up |
| Broker / verbs | repo templates | `/etc/disjorn-broker/*` | sudoedit; verbs.toml is re-read per request |
| Metrics, errorlog | `harness/…` | **the repo** (unit ExecStart points at it) | nothing — next timer tick |

**The trap, concretely.** `house_memory` is editable-installed into
`/home/plink/bots/claudette/.venv` pointing at the repo — but that is the
**host** venv. Her **container** mounts `/usr/local/lib/disjorn/house_memory`
at `/opt/house_memory`. Same library, two paths, and only one of them is what
runs. Verified 2026-08-04 by `podman inspect`; asserted wrongly before that.

## Whose filesystem is a path on?

Prefix every path you quote with one of these. It costs four characters and
has already been worth several hours.

| Sticker | Means | Example |
|---|---|---|
| `host:` | the Debian box, as plink | `host:/home/plink/Disjorn/Disjorn` |
| `claudette-ctr:` | inside her running container | `claudette-ctr:/opt/house_memory` |
| `gable-ctr:` | inside a summon container | `gable-ctr:/opt/spine` |
| `res-vol:` | a resident's home VOLUME on the host | `res-vol:/home/res-gable/resident-home` |

`/home/resident/...` is **always** a container path. The host equivalent is
`/home/res-<name>/resident-home/...`. These two are the pair that keeps
costing afternoons.

## Read-only walls, and why they exist

Residents can read but never write: `/opt/disjorn` (the repo mirror),
`/opt/spine`, `/opt/house_memory`, `/config`. That is not bureaucracy — it is
the whole reason a resident can propose a change to its own prompt without
being able to make one. Every `:ro` in the mount table is load-bearing.

## Junk you can ignore

- `.claude/worktrees/` — abandoned agent worktrees on merged branches.
  Gitignored. They inflate any "how many copies of this file exist" count.
- `res-vol:/home/res-gable/resident-home/disjorn` — Gable's own clone.
- `~/SPECS-DRAFT-*.md` in a resident's home — superseded once the spec lands
  in `SPECS/`.

## The four-step sanity check when something "should be fixed but isn't"

1. Did you edit the writable copy, or a derivative?
2. Who holds those bytes in RAM? (see `deploy/DEPLOY-CHEATSHEET.md` — read
   per request / composed per summon / **imported at start**)
3. If imported at start: did you copy AND restart?
4. Prove it from the thing that runs, not from the disk — for a resident,
   `podman exec` and import the module, or hail it in-channel.
