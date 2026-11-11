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
  `bundles/{claudette,spine,disjorn}.bundle` (`--all`), `manifest.json`,
  `apps/<id>/{repo.bundle,live,live.prev,preview}` (see Apps below).
- Live: `server/data/uploads`, `server/data/assets`, `server/.env`, both
  resident homes whole, `/var/lib/disjorn-broker`, `/var/log/disjorn-broker`,
  `/etc/disjorn-broker`, `/home/plink/resident-config`,
  `/srv/disjorn-resident-config`.

`manifest.json`: `messages`, `migration`, `bundles.<name>` (ref → sha),
`claudette.{count,export_sha256}`, `gable.{memory_files,memory_md_sha256}`,
`apps.apps.<id>` (`bundle_sha256`, `head`, `live_commit`, and
`{files,bytes}` for each of `live`, `live_prev`, `preview`; never a file
name), `apps.warnings` (name → reason).
The drill compares only against the manifest, never live state.

Claudette's export is taken from a copy of her chroma dir (`sqlite3 .backup` of
`chroma.sqlite3` + the segment files), never the live dir. The drill imports it
into a scratch `MemoryStore` with Claudette's interpreter
(`/home/plink/bots/claudette/.venv/bin/python`), re-exports, and requires `==`
with the file.

`lib.sh` holds the shared paths, `post`, and manifest helpers; every path is
overridable from the environment (the tests rely on it).

## Apps

`apps.py stage` runs inside `snapshot.sh` and copies each app into the stage,
so the manifest describes exactly the bytes restic then reads, and an app
that changes or is deleted during the run cannot fail `restic backup`.

| Where | In the snapshot | Why |
|---|---|---|
| `/srv/apps-git/<id>.git` | yes, `repo.bundle` (`--all`: branches, `refs/disjorn/live`, `refs/disjorn/live.prev`) | the only copy of an app's source and history |
| `/srv/apps-www/<id>/live`, `live.prev` | yes, as files | what viewers are served; an app published before the live ref existed has no commit that names it |
| `/srv/apps-www/<id>/preview` | yes, as files | not always HEAD: it keeps git-ignored files the work tree held, and a halted turn commits without republishing it. Publish copies it to `live` |
| `/srv/apps/<id>` (work tree) | no | `checkout` of HEAD rebuilds it; only git-ignored leftovers are lost |
| `/srv/apps-turns` | no | per-turn logs and results |
| `/srv/apps-www/.staging`, `/srv/apps-quarantine` | no | transient staging; quarantined files hold a credential by definition |

- `git` runs on a git dir as the dir's owner (`runuser -u res-appsbuilding`),
  never as root: the seat can write that repo's config. Nothing under `/srv`
  is written, chowned or chmodded.
- Web roots are copied as regular files and directories only, each opened
  relative to its parent with `O_NOFOLLOW`. A symlink inside a root is left
  out; a root that is itself a symlink is a warning.
- An app whose directories vanish mid-run (a delete) is left out with a
  logged note and the run goes on.
- A git dir that is half-written, unreadable or will not bundle, a web root
  that cannot be read, or a missing `/srv/apps-git` or `/srv/apps-www`, is a
  `WARNING` in the unit's log and an entry in `apps.warnings`. Only the part
  that failed is left out; the app's other parts are still staged. The
  snapshot then carries the extra restic tag `apps-partial`; `freshness.sh`
  posts a WARNING for it every day it is the newest, and the drill's
  headline reads `PARTIAL` instead of `PASS`. Neither fails its unit.
- The drill runs `git bundle verify` on every recorded bundle, compares its
  sha256, clones it and requires the recorded `HEAD` and `refs/disjorn/live`
  commits in the clone; it recounts each staged web root against the
  manifest. Any mismatch, or a manifest with no `apps` section, is RED. The
  PASS line ends `apps: N bundles verified, N live roots matched`.

### Restore one app after a mistaken delete

A delete removes the app from the next nightly on. Take the newest snapshot
from BEFORE the delete (`restic snapshots --tag disjorn-nightly`); retention
is 7 daily, 4 weekly, 12 monthly. Work done after that snapshot is gone.
Everything below is a root shell (`sudo -i`); set `ID` and `SNAP` first.

```sh
ID=<app id>; SNAP=<snapshot id>; SEAT=res-appsbuilding
S=$(mktemp -d /var/tmp/app-restore.XXXXXX); A=$S/var/lib/disjorn-backup/stage/apps/$ID
( set -a; . /etc/disjorn-backup/restic.env
  restic restore "$SNAP" --target "$S" --include "/var/lib/disjorn-backup/stage/apps/$ID" )
ls "$A"                                   # repo.bundle, and live / live.prev / preview if it had them
test ! -e /srv/apps-git/$ID.git -a ! -e /srv/apps/$ID -a ! -e /srv/apps-www/$ID || echo "STOP: not deleted"

git init -q --bare -b main /srv/apps-git/$ID.git
git -C /srv/apps-git/$ID.git fetch -q "$A/repo.bundle" '+refs/*:refs/*'
git -C /srv/apps-git/$ID.git config core.bare false
git -C /srv/apps-git/$ID.git config user.name apps-builder
git -C /srv/apps-git/$ID.git config user.email apps-builder@disjorn.local
chown -R $SEAT:$SEAT /srv/apps-git/$ID.git && chmod 0700 /srv/apps-git/$ID.git

install -d -o $SEAT -g $SEAT -m 0750 /srv/apps/$ID
( cd / && runuser -u $SEAT -- env HOME=/home/$SEAT git --git-dir=/srv/apps-git/$ID.git \
      --work-tree=/srv/apps/$ID -c core.hooksPath=/dev/null checkout -q -f )

install -d -o $SEAT -g $SEAT -m 0755 /srv/apps-www/$ID
for r in live live.prev preview; do [ ! -d "$A/$r" ] || cp -r "$A/$r" /srv/apps-www/$ID/$r; done
chown -R $SEAT:$SEAT /srv/apps-www/$ID && chmod -R u=rwX,go=rX /srv/apps-www/$ID
rm -rf "$S"
```

Owners and modes, as the launcher and harvest make them: `<id>.git` 0700,
the work tree 0750, `/srv/apps-www/<id>` and everything under it 0755 / 0644,
all `res-appsbuilding:res-appsbuilding`. An app with no `repo.bundle` had no
commits: skip the fetch and the checkout. The work tree must hold no `.git`
entry, and `git clone` is not used because it would leave an `origin` remote.

The database, for a delete made by the owner's Delete app
(`SPECS/2026-11-11-apps-delete.md`: it leaves the `apps` row as a tombstone
and removes the rest). After a disk loss with the database intact, none of
this applies.

- As plink, never root (root-owned `-wal`/`-shm` lock the server out):
  `sqlite3 server/data/disjorn.db "UPDATE apps SET deleted_at = NULL, deleted_builds = 0 WHERE id = '<id>'"`.
  Name, status and visibility were kept on the row, so the app is back on
  its owner's list as it was once the client reloads. If the row says live
  and the snapshot held no `live`, the owner publishes again.
- Its `app_shares` and `user_apps` rows were deleted: re-share by hand, or
  copy those rows for this `app_id` out of the snapshot's `stage/disjorn.db`.
- Its build sessions, their build chat channels and messages, and their
  stage events are gone. They exist only in the snapshot's `disjorn.db`, and
  restoring that file rolls the whole house back to the snapshot. The owner
  starts a new build session instead.

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

Backblaze B2 goes through its S3-compatible API, which is restic's recommended path for B2:

```sh
RESTIC_REPOSITORY=s3:https://<bucket Endpoint, e.g. s3.us-east-005.backblazeb2.com>/<bucket>/<path>
AWS_ACCESS_KEY_ID=<B2 keyID>              # an application key limited to that bucket
AWS_SECRET_ACCESS_KEY=<B2 applicationKey>
```

On this host `CLAUDETTE_MEMORY_DIR` is `/home/res-claudette/resident-home/memory/chroma_data` and
`CLAUDETTE_RETRIEVAL_LOG` is `/home/res-claudette/resident-home/memory/memory_retrieval.jsonl`, the host
side of what her container sees under `/home/resident/memory`.

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
(`tests/fake_*.py`), tmp house and tmp apps roots (`APPS_GIT_ROOT`,
`APPS_WWW_ROOT`), snapshot → drill → freshness end to end.
