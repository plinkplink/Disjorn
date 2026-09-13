# Spec: APPS — the app repo's git dir leaves the container mount (backlog #24)

## Request
- **Verbatim**: "@Gable, your tokens are reset for the week, let's knock out #24 and keep moving." (plink, #custodian #2580); ruling on shape: "Gable can write the spec, you can review, and BuildGable can execute tomorrow when account usage unlocks." (#2569)
- **Requester**: plink
- **Origin**: #custodian #2546 (Gable: `.git` pointer-file remix BLOCK), #2557 (Gable: alternates + symlinked ref, `core.fsmonitor` host execution), card backlog-24 (opened #2553, widened #2559). Claudette's draft #2570 is raw material for this file; where this file differs from it, this file wins and the difference is stated.

## Problem
An apps-builder turn writes `/work`, and `/work` is `/srv/apps/<app-id>` mounted rw (`run-apps.sh:156`). The app's repository lives at `/srv/apps/<app-id>/.git`, inside that mount, so the turn — the party this design models as possibly prompt-injected — writes the repository's own metadata, and every host-side git command reads it afterwards. In prod since stage 2 slice (i). Three vectors, all probed by me on git 2.47.3 (#2546, #2557):

1. **Host code execution as `res-appsbuilding`, outside the container.** A repo-local `core.fsmonitor=<cmd>` in `.git/config` ran under the harvest's own `git -c core.hooksPath=/dev/null -C <repo> add -A` (`apps_harvest.py:182`, inside the transient unit on the host). `hooksPath=/dev/null` closes hooks and nothing else; `filter.*.clean` (fires on `add`), `core.sshCommand`, `uploadpack.packObjectsHook`, `diff.*.textconv` are the same door. Blocklisting keys is the wrong shape: the list is git's to extend.
2. **Cross-app history read.** A real `.git` directory (passes stage 3's `_is_a_real_repo`) with a turn-written `objects/info/alternates` naming app B's object store plus a symlinked `refs/heads/<branch>` or `packed-refs` makes `remix`'s `clone --no-local` yield B's files and history. Needs only B's app id. The leak is in upload-pack on the parent; the child carrying no alternates proves nothing.
3. **The pointer file.** `.git` as a 40-byte `gitdir:` file. Closed for remix at `4573c4a`; `apps_harvest.ensure_repo` (`apps_harvest.py:215`) still decides "already a repo" by `.exists()` and is open.

Each fold so far closed a shape. The class is: we validate a path the adversary writes. The fix is that the adversary stops being able to write it.

## Agreed UX
None visible. Turns, previews, Live, Revert and Remix behave as today. A turn that writes a `.git` entry into `/work` now has it quarantined and named in `result.json`; nothing else a user sees changes.

## Architecture notes (D-series; each sentence is strikeable at review)

- **D1 — a host-only git root.** `/srv/apps-git/`, `0750 res-appsbuilding`, created by `10-appsbuilding.sh` beside the four existing roots (`10-appsbuilding.sh:240-246`). Per app: `/srv/apps-git/<app-id>.git/`, `0700`. `/srv/apps/<app-id>` stays the work tree and the only persistent rw mount. Nothing under `/srv/apps-git` is mounted into any container, ever; the podman argv in `run-apps.sh` is pinned by a test asserting no `-v`/`--mount` source resolves under it. The launcher gains the constant `GIT_ROOT` beside `APPS_ROOT` (`disjorn-apps-launch:165`) and sets `APPS_GIT_ROOT` for the wrapper; the wrapper passes `git_dir` in the harvest's JSON spec.
- **D2 — every git invocation names both trees explicitly.** `apps_harvest.git` / `git_bytes` and the launcher's `_git` build `git --git-dir=<G> --work-tree=<W> -c core.hooksPath=/dev/null …`. No path the turn can write is consulted to find the repository, so `.git` in `/work` is never read, whatever it is. The harvest's env gains `GIT_CONFIG_GLOBAL=/dev/null` (the launcher's `_git` already sets it, `disjorn-apps-launch:976`; the harvest sets only `NOSYSTEM` and must not be the weaker of the two).
- **D3 — `ensure_repo` creates a split repo.** End state, pinned by test: `G` exists and holds `HEAD`; `core.bare=false`; `core.worktree` unset (the work tree is argv, never config); **no `.git` entry of any kind in `W`**. "Already a repo?" becomes "does `G/HEAD` exist" — a fact about the host-only tree. The builder picks the invocation git honours (note: `--separate-git-dir` writes a `.git` pointer file into `W`, which is exactly the entry this spec forbids; if used, it is removed in the same function and the test proves it gone).
- **D4 — remix reads the host-only tree.** `_is_a_real_repo` is deleted, not kept — there is nothing left in `W` to validate. Its replacement checks `G`: real directory by lstat, realpath under `/srv/apps-git`, holds `HEAD`; in both `preflight_remix` (root) and `remix_app` (seat). Clone stays `--no-local --no-hardlinks` (two separately-writable apps must share no object file, alternates or not). The child gets `/srv/apps-git/<child>.git` and a work tree with no `.git` entry; the lineage commit `remix of <parent>` is unchanged.
- **D5 — a `.git` entry in `/work` is a stray, quarantined as a class.** Git's directory walk skips any entry named `.git` at every level (dir.c `treat_path`; stated from source memory, not re-run on this seat — `~/specs-drafts/probe-stray-git.sh` is the 20-line probe, and tests 1, 3 and 8 pin it either way), so a turn-written `/work/.git` — file, dir or symlink — is invisible to `status`, `add`, `clean` (even `-ff`) and to the harvest's porcelain-driven scan, and rsync's `--exclude .git` keeps it out of the preview. Invisible is not harmless: it is the one place in the tree a payload sits unscanned across turns. So the harvest, before the scan, lstat-walks `W` for entries named `.git`, moves each to `<quarantine>/<app>/<turn>/stray-git/<relpath>` (never followed), and records `stray_git: [paths]` in `result.json`; the turn otherwise proceeds normally. The watcher's `_changed_since` stops skipping `.git` (its reason — `git init` touching `W/.git` at t=0 — no longer exists). `preview_argv`'s `--exclude .git` stays. This replaces #2570's D5; `git clean -ff` is not taken, because git never lists a `.git` entry for `clean` to remove.
- **D6 — one-time migration**, `harness/keyboard/11-apps-gitdir-migrate.sh`, root, idempotent, refuses while any `disjorn-apps-*` unit is active. Per `/srv/apps/<id>`:
  1. **Audit first, printed, before anything moves**: for each existing `.git/config`, every key outside the set `core.{repositoryformatversion,filemode,bare,logallrefupdates}`, `user.*`, `init.*` is printed verbatim; non-sample files under `hooks/`; `objects/info/alternates` contents; every symlink anywhere under `.git` (`find -type l`). One block per app to the keyboard, kept in the merge record. "Was this hole used?" is answerable today and not after the rewrite.
  2. `.git` not a real directory (pointer file, symlink): not followed. Moved to `/srv/apps-quarantine/<id>/gitdir-migration/`; fresh `G` init'd per D3; first commit `history discarded at migration: .git was a pointer`.
  3. `.git` a real directory with any symlink anywhere under it: quarantined whole, same as 2.
  4. Otherwise: `mv .git /srv/apps-git/<id>.git`, then **sanitize before any git command touches it**: `config` deleted and rewritten minimal (format version, `core.bare=false`, `user.name`, `user.email`); `hooks/` deleted; `objects/info/alternates` deleted; then `git --git-dir=G fsck --no-dangling` (fsck reads config, so it runs last). An fsck failure quarantines whole and re-inits, printed.
  5. Result: `G` `0700 res-appsbuilding`, `W` with no `.git` entry, `W` mode `0750` unchanged. Apps with no `.git` at all are left alone; their first turn creates `G`.
- **D7 — the wall, written once.** A paragraph in `run-apps.sh`'s header and `apps_harvest`'s docstring: the turn's writable surface is the work tree only; git metadata is host-only by location; a shape check on a path the turn can write is not a boundary and is never written down as one (the `gate.py` #2561 discipline). Work-tree files that name git machinery (`.gitattributes` filters, `.gitmodules`) reference drivers the host-only config never defines, and stay inert for that reason, not by a scan.

## Non-goals
Per-app origin (#25). Blocklisting config keys. Any change to the secret scan, the preview publish discipline, the quarantine contract, or the sudoers file. Further sandboxing of git (seccomp, a container for the harvest) — this spec ends the class for the mount; that would be its own card.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: builder — `harness/cc/apps/{apps_harvest.py,run-apps.sh,disjorn-apps-launch}`, `harness/keyboard/10-appsbuilding.sh`, new `11-apps-gitdir-migrate.sh`, `harness/cc/tests/test_apps_{harvest,launch}.py`.
- **Review owner**: Gable (lane rule). Spec review before confirm: Claudette, cold read, per plink #2569 — that is review of this document, not of the build, and does not enter this box.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: keyboard (BuildGable seat) under an Opus 5 seat, per plink #2565/#2569. Not built by the lane whose hole it is.

## Expected diff tier
Tier 2: root launcher, provisioning script, a migration that moves data under `/srv`. Sudoers untouched (assert by diff stat at review).

## Tests
Acceptance is not "fsmonitor is blocked". It is: **a turn writes `.git` and the write lands nowhere the host git ever reads.** Each new test reverted-and-rerun red against pre-fold code before it counts.

1. Plant `W/.git/config` with `core.fsmonitor` naming a marker script; full harvest; marker never runs; the turn's real content commits normally; `stray_git` names the entry and it sits in quarantine.
2. Plant `W/.git` as a real directory with `objects/info/alternates` naming app B's `G` plus a symlinked ref; remix yields the parent's own history only; B's objects absent from the child (object-id set difference, not "no alternates file").
3. Plant `W/.git` as a `gitdir:` pointer at another app's `G`; `ensure_repo`, harvest and remix ignore it (nothing refuses by shape, because nothing looks); `ensure_repo` is a no-op decided on `G/HEAD`.
4. Argv pins: the harvest's and launcher's git argv carry `--git-dir` and `--work-tree`; the podman argv mounts nothing under the git root; the harvest env carries `GIT_CONFIG_GLOBAL=/dev/null`.
5. `ensure_repo` on a fresh app creates `G` and no `.git` entry in `W`; second call creates nothing.
6. Remix: child `W` has no `.git`; child history = parent's + lineage commit; parent and child share no object file (inode comparison).
7. Migration on a fixture tree: pointer-file app quarantined + re-init'd; symlink-inside-git app quarantined whole; honest app moved and sanitized (config minimal, hooks gone, alternates gone), history intact; second run a no-op; refuses with an active unit; audit block printed for each.
8. Watcher: a turn whose only write is `W/.git/x` still fires `scaffolded`.
9. Every existing `test_apps_harvest.py` / `test_apps_launch.py` assertion passes unchanged — the split is invisible to the scan, `no_changes`, halted commits and the preview rotate. `test_ensure_repo_is_idempotent…` (`test_apps_harvest.py:107`) asserts `(repo/".git").is_dir()` today and is rewritten to assert the opposite.

## Deploy
Apps idle (no `disjorn-apps-*` unit active). Tar `/srv/apps` and back up the DB first. The five files deploy together — `10-appsbuilding.sh` (new root), `11-apps-gitdir-migrate.sh`, `apps_harvest.py`, `run-apps.sh`, `disjorn-apps-launch` — a harvest and a wrapper from different sides of this change is a turn that cannot find its repository. Order: `10-appsbuilding.sh` (creates `/srv/apps-git`), install the three `/usr/local/lib/disjorn` files, run `11-apps-gitdir-migrate.sh`, keep its audit output in the merge record, then one proving turn on a scratch app and one remix. No apps image rebuild: nothing inside the container changes. Rollback: move the git dirs back under `W/.git`, revert the five files; the audit output is kept either way.

## Token estimate
One keyboard build, Opus 5. Comparable to stage 3's builder strip (three files + tests + one script).

## Confirm record
- **Confirmed by**:
- **#custodian seq**:
- **Confirmed at**:
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
