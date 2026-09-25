# harness/backup — nightly encrypted offsite backup + restore drill

Spec: `SPECS/2026-09-23-backup-and-restore-drill.md`. The USB drive stays the
fast local copy; this is the offsite one.

## Units (all root, all `OnFailure=disjorn-backup-alert@%n.service`)

| Unit | When (UTC) | Does |
|---|---|---|
| `disjorn-backup-snapshot` | nightly 03:30 | `snapshot.sh`: stage, `restic backup`, `forget --prune` 7d/4w/12m |
| `disjorn-backup-drill` | Sun 05:00 | `drill.sh`: restore newest snapshot to scratch, check vs manifest, post PASS/RED |
| `disjorn-backup-check` | 1st of month 06:00 | `restic check --read-data-subset=5%` |
| `disjorn-backup-freshness` | daily 12:00 | `freshness.sh`: post RED if newest snapshot > 26h old or missing |
| `disjorn-backup-alert@` | on failure | posts `backup RED: <unit> failed` + last 8 log lines |

All posts go to #custodian as the broker bot (`brokerd._sdk_transport`, the
file-proposal transport), via `server/.venv/bin/python` and
`/etc/disjorn-broker/broker.toml`. A drill or freshness RED also fails the
unit, so each RED arrives twice: the detail, then the alert.

**Cadence rule:** the drill period must stay shorter than the shortest
retention tier (7 dailies), or `forget --prune` removes the last good dailies
before a drill notices.

## What a snapshot holds

- `$STAGE` (`/var/lib/disjorn-backup/stage`): `disjorn.db` (`sqlite3 .backup`),
  `claudette/memory-export.json` + `memory_retrieval.jsonl`,
  `bundles/{claudette,spine,disjorn}.bundle` (`--all`), `manifest.json`.
- Live: `server/data/uploads`, `server/data/assets`, `server/.env`, both
  resident homes whole, `/var/lib/disjorn-broker`, `/var/log/disjorn-broker`,
  `/etc/disjorn-broker`, `/home/plink/resident-config`,
  `/srv/disjorn-resident-config`.

`manifest.json`: `messages`, `migration`, `bundles.<name>` (ref → sha),
`claudette.{count,export_sha256}`, `gable.{memory_files,memory_md_sha256}`.
The drill compares only against the manifest, never live state.

Claudette's export is taken from a copy of her chroma dir (`sqlite3 .backup` of
`chroma.sqlite3` + the segment files), never the live dir. The drill imports it
into a scratch `MemoryStore` with Claudette's interpreter
(`/home/plink/bots/claudette/.venv/bin/python`), re-exports, and requires `==`
with the file.

`lib.sh` holds the shared paths, `post`, and manifest helpers; every path is
overridable from the environment (the tests rely on it).

## Install (plink, at the keyboard)

```sh
sudo apt install restic sqlite3
grep -rn data_dir /home/plink/bots/claudette      # settle CLAUDETTE_MEMORY_DIR
sudo install -d -m 0700 /etc/disjorn-backup
sudoedit /etc/disjorn-backup/restic.env            # then: chmod 0600, root:root
```

`restic.env`:

```sh
RESTIC_REPOSITORY=<destination URL>
RESTIC_PASSWORD=<long random>
CLAUDETTE_MEMORY_DIR=<host path of her chroma dir>
CLAUDETTE_RETRIEVAL_LOG=<host path of her memory_retrieval.jsonl>
# plus any credentials the destination backend needs
```

Key escrow: print `RESTIC_PASSWORD` and keep it off-box. Without it the
offsite copy is unreadable.

```sh
sudo sh -c 'set -a; . /etc/disjorn-backup/restic.env; restic init'
sudo cp harness/backup/disjorn-backup-*.service harness/backup/disjorn-backup-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now disjorn-backup-{snapshot,drill,check,freshness}.timer
```

## On demand, and after every DB migration

```sh
sudo systemctl start disjorn-backup-snapshot.service && sudo systemctl start disjorn-backup-drill.service
```

## Acceptance

First drill run by hand, witnessed in #custodian: every check passes, plus a
live recall of a known Claudette memory and a read of a known Gable memory
file from the restore. Then kill one unit on purpose and confirm the red post
lands.

## Tests

`python3 -m pytest harness/backup/tests -q` — stub `restic`/`sqlite3`
(`tests/fake_*.py`), tmp house, snapshot → drill → freshness end to end.
