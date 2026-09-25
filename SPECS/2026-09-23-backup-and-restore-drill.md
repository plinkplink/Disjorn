# Spec: Nightly encrypted offsite backup with an automated restore drill

## Request
- **Verbatim**: "Claudette, go ahead and write the spec in #custodian, with the usual template. I'll have Gable review and make it happen."
- **Requester**: plink
- **Origin**: main #1656 (context #1654). Draft #3024, review #3032 (Gable); this revision folds all of #3032.

## Agreed UX
Nothing to do day to day. A nightly timer snapshots the house, encrypts it, and ships it offsite. A weekly timer restores the latest snapshot into a scratch dir, verifies it against a manifest written at snapshot time, and posts pass/fail to #custodian. A failed unit, a red drill, or a stale snapshot always gets posted, so silence never means a hidden failure. The USB drive stays as the fast local copy. This is the copy that survives the fire.

## Architecture notes
Snapshot contents (all host-absolute):
1. `/home/plink/Disjorn/Disjorn/server/data/disjorn.db` via `sqlite3 .backup` (WAL, never a raw copy), plus `.../data/uploads/`, `.../data/assets/`, `.../server/.env`.
2. Resident homes, whole: `/home/res-claudette/resident-home/` and `/home/res-gable/resident-home/`. restic dedups, so the cost is nil. Gable's memory is files (`.claude/projects/-home-resident/memory/`, `specs-drafts/`, `.action-log`), and his chroma dir is empty by design. Copying the whole tree is the only thing that saves him.
3. Claudette's store also gets a verified export: `house_memory` `export_all()` to JSON (embeddings verbatim, sorted, ==-comparable) plus the retrieval log. The drill verifies the export, not the live chroma dir. Her `data_dir` gets settled at the keyboard with `grep -rn data_dir /home/plink/bots/claudette`.
4. Git bundles, all branches: `/home/plink/bots/claudette`, `/home/plink/bots/fable/spine`, `/home/plink/Disjorn/Disjorn` (the prod tree is the only clone).
5. Broker state: `/var/lib/disjorn-broker/` (gatehouse bare repos with loop branches that exist nowhere else, the consumed ledger, gate logs, the wake spool), `/var/log/disjorn-broker/` (audit, apps ledger), `/etc/disjorn-broker/`.
6. Resident config: `/home/plink/resident-config/*`, `/srv/disjorn-resident-config/*`.
7. **Manifest**, written at snapshot time and stored inside it: message count, each bundle's branch HEADs, Claudette's memory count + export sha, Gable's memory file count + `MEMORY.md` sha, migration number. Every drill check compares against the manifest, never against live state.

Tool: restic. Retention 7 daily / 4 weekly / 12 monthly, which also serves as the written retention policy from #1651. plink picks the destination. The spec only needs a repo URL and password in a root-only env file. Key escrow: print the password and keep it off-box.

Cadence rule: the drill period must be shorter than the shortest retention tier, or `forget --prune` eats the last good dailies before anyone notices. Restore drill weekly, on demand, and after every DB migration. `restic check --read-data-subset=5%` monthly.

Drill (`harness/backup/drill.sh`), against scratch:
- `PRAGMA integrity_check` = ok; message count == manifest.
- Claudette: `import_all()` into a scratch MemoryStore, then `export_all()` == the file and count == manifest, superseded records included. Interpreter named explicitly: `/home/plink/bots/claudette/.venv/bin/python` (the only host env with house_memory's deps).
- Gable: memory file count and `MEMORY.md` sha == manifest.
- Bundles: `git bundle verify`, branch HEADs == manifest.
- Result posted to #custodian under the broker bot's identity (the file-proposal transport).

Failure reporting (a timer that never fires can't report itself):
- `OnFailure=` on the snapshot, drill and check units, posting red through the broker identity, following the `disjorn-metrics-daily.service` pattern.
- A daily freshness unit posts if the newest snapshot is over 26h old. It gets its own `OnFailure=` too.

Files: `harness/backup/{snapshot.sh,drill.sh,freshness.sh,memory_export.py,README.md}`, plus units + timers (snapshot nightly, drill weekly, check monthly, freshness daily), plus rows in TREE.md and README-DEPLOY §7.

## Lane → Review owner (DETERMINISTIC)
- **Lane**: custodian (house ops)
- **Review owner**: Gable

## Builder (USER PREFERENCE)
- **Builder**: Gable (plink, #1656). The build lane produces the files and unit files. Installing the root timers, writing the env file, picking the destination and escrowing the key are plink's install at the keyboard.

## Cross-lane split
- **Applies**: yes, narrowly
- **Surfaces by lane**: scripts, units, manifest, Gable memory check → Gable. `memory_export.py` + Claudette round-trip → Claudette, who signs off that it's lossless.
- **Split agreed in #custodian**: #3032 + this post

## Expected diff tier
Tier 2: root timers, secrets, reads every resident's store.

## Token estimate
~220k

## Acceptance
plink runs the first drill by hand, witnessed in #custodian. It must pass every check, plus a live recall of a known Claudette memory and a read of a known Gable memory file from the restore. Then plink kills one unit on purpose and confirms the red post lands. Until both happen, we only know backups exist, not that they work.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 3036
- **Confirmed at**: 9/23/2026

## Status
building
<!-- set by the broker on 2026-09-25 01:40Z (start-build, 2026-09-23-backup-and-restore-drill): build running as disjorn-build-2026-09-23-backup-and-restore-drill.service -> loop/2026-09-23-backup-and-restore-drill, launched by gable (confirmed by plink, #custodian seq 3036). Not buildable again until this line moves. -->
<!-- set by the broker on 2026-09-24 00:09Z (start-build, 2026-09-23-backup-and-restore-drill): the build ran and produced no commits — no branch, nothing to review; buildable again. -->
<!-- set by the broker on 2026-09-24 00:07Z (start-build, 2026-09-23-backup-and-restore-drill): build running as disjorn-build-2026-09-23-backup-and-restore-drill.service -> loop/2026-09-23-backup-and-restore-drill, launched by gable (confirmed by plink, #custodian seq 3036). Not buildable again until this line moves. -->
