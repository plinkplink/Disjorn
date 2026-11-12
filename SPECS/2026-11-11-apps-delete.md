# Spec: The owner can delete an app

<!--
Governing plan: BUILD-LOOP.md. Style: COMMS.md. Built ahead of its confirm so
it can be offered as a finished branch.
-->

## Request
- **Verbatim**: "Add the ability to delete unused/outdated APPS."
- **Requester**: plink
- **Origin**: backlog item 65, handed to the keyboard seat for the 2026-11-11
  sprint.

## Problem
- Until Keep building existed, every iteration on a live app made a new
  "(remix)" app. The owner's list now holds stale ones and nothing removes
  them.
- An app is more than a row: a work tree, a git dir, up to three served
  roots, turn records and one build chat per session. Nothing removes any of
  those either.

## PRIVILEGED SURFACE: read this first
This build adds a sixth sudoers alias and the first launcher mode that
destroys data.

- **The alias**: `DISJORN_APPS_DELETE`, `^delete [a-z2-7]{12}$`. One app id,
  anchored at both ends, the same shape as `publish` and `revert`. No path,
  no session id, no second argument.
- **What `plink` gains** (the uid the server and the broker run as): the
  ability to remove any one app's trees by naming its id. Before this the
  worst a serving mode could do was serve an app's own files a deploy early
  or late. Nothing in the product restores a removed app. Since the
  backup-covers-apps merge the nightly snapshot holds each app's git bundle
  and web roots, so a deleted app stays in the offsite repository until
  retention prunes it (7 daily, 4 weekly, 12 monthly), and
  `harness/backup/README.md` has the by-hand restore.
- **What it cannot reach**: anything outside `/srv/apps-www/<id>`,
  `/srv/apps-www/.staging/<id>`, `/srv/apps/<id>`, `/srv/apps-git/<id>.git`
  and the turn dirs under `/srv/apps-turns` whose own record names `<id>`.
  `/srv/apps-quarantine` is never opened.
- **Who removes**: `res-appsbuilding`, inside a transient unit, exactly as
  publish, revert and remix do. Root validates the one id, builds a fixed
  `systemd-run` argv and execs. Root removes nothing.
- **What the seat does besides removing**: it unlinks a symlink found at one
  of those names, without following it, and it adds the owner's rwx bits to
  real directories inside a tree it is removing when a build left them
  closed. It changes no mode outside that tree and none through a link.
- **Not serialised in the launcher**: a delete, a publish and a remix of one
  app run in units with different names, so the launcher does not keep them
  apart. That rests on the server: `_mark_app_deleted` refuses while a
  publish claim is held, and no publish or remix can start on a deleted row.

## Agreed UX
Proposed. Nothing below has been agreed in #custodian yet.

**Who**
- The app's owner, and nobody else. Not an admin: no owner verb in
  `routers/apps.py` has an admin override, and this build adds none
  (answer 1). Never a bot.

**Where**
- The app's card gets **Delete app…** for its owner, set apart from the
  card's other buttons, in the danger colour.
- The owner's rows in the Apps list get a `⋯` button that opens the card.
  It is needed because a click on the owner's own draft, or on a row with a
  session open, goes to the build modal and never showed the card. Those are
  the apps most often deleted. It shows on hover or keyboard focus, and
  always on a touch screen.
- There was no row menu to add an item to.

**The confirm dialog**
- Title: "Delete «name»".
- "This deletes «name» for good: the app, its build chat and history, and
  its preview and live pages. Nothing brings it back."
- One line when other people can reach it:
  - live and public: "It is live and public: everyone in the house loses it."
  - live and shared: "It is live and shared: the people it was shared with
    lose access."
  - live and private: "It is live: its link stops working."
- With a session open: "Its open build session is closed. A build that is
  running has to be stopped first."
- "Remixes made from it, yours or anyone else's, stay as apps of their own."
- Buttons: Cancel, and **Delete app** in the destructive style. No typing to
  confirm.
- A refusal or a failure is shown in the server's words and the button
  becomes **Try again**. If the server had already deleted the app, the
  dialog says so: off everyone else's list, no new link can be made, files
  still on disk, a link opened earlier may still work, and the row in the
  Apps list now offers Finish deleting.

**A delete that did not finish**
- The app stays in its owner's Apps list, greyed, with one action in place
  of the status chip: **Finish deleting**. It is there on every device and
  after a reload, until the delete has finished.
- A click sends the delete again. The label reads "Deleting…" meanwhile; a
  refusal is said out loud in the server's words, as a failed session start
  already is.
- Nobody else is shown the row. It has no card, no `⋯`, and nothing but the
  delete answers for it.

**After**
- The row leaves the owner's list in every tab and on every device, by a
  new `app_delete` frame.
- The same frame reaches people it was shared with and people who had it on
  their menu. Their row leaves, and a card for it in a channel falls back to
  the plain link.
- A build modal open on the app in another tab closes.
- Remixes of it are untouched.

**A build in progress**
- A running turn refuses the delete: 409, "A build is running on this app;
  stop it first, then delete the app." Nothing changes.
- A publish in flight refuses it the same way.
- An open session with no turn running is closed by the delete.

## Architecture notes

### Soft delete of the row, hard delete of everything else
- `apps.deleted_at` (migration 025). The row stays as a tombstone.
- Why the row stays:
  - `apps.parent_app_id REFERENCES apps(id)` has no delete action, and
    foreign keys are on. Removing a parent row with a remix alive would fail,
    or would have to rewrite the child's lineage.
  - `_mint_app_id` checks the table, so a tombstoned id is never minted
    again. An id is a URL path segment and a directory name; reuse would
    hand a new app an old link.
  - A second DELETE needs the row to know whose app it was.
- Why nothing else stays: the request asks for the apps to be gone, the chat
  is the owner's private history, and the files are the bulk. No grace window
  and no undelete.
- The row keeps its name, status and visibility as they were. Its
  `app_shares` and `user_apps` rows are deleted.
- `apps.purged_at` is set once the helper has succeeded and the build chats
  are gone. Deleted and not purged is a delete that stopped part way.

### Order, in the request
1. **One transaction** (`_mark_app_deleted`):
   - 409 if any session of the app has a publish claim
     (`live_publishing_at`) or a running turn (`_running_turn`, the function
     the ended-session gate uses);
   - count the owner's builds of today on this app into `deleted_builds`;
   - close every session with `_close_session`;
   - delete the share and menu rows;
   - set `deleted_at`.
2. Publish `app_delete` to the owner, the shared-with and the menu holders.
3. **The helper**: `disjorn-apps-launch delete <id>`, synchronously. Its
   first act is removing `/srv/apps-www/<id>`.
4. **The build chats**: every `app_build` channel of the app's sessions goes
   through `channels.destroy_channel`, the function `DELETE /channels/{id}`
   and leaving a bot DM already use. Each writes its `channel_deletions` row
   attributed to the user and publishes `channel_delete` to the owner and the
   builder bot. The cascade removes the app's sessions and stage events.
5. Set `purged_at`, publish `app_delete` once more to the owner alone, and
   answer `{"ok": true}`.

A failure at 3 or 4 leaves the app deleted and not purged. The owner's next
DELETE skips step 1 and runs 3 to 5 again. A deleted app answers 404 to
everyone else.

### The unfinished delete, as the owner sees it
- `GET /apps` returns the caller's own deleted, unpurged rows beside their
  living apps, with `status: "deleting"`. That status is made in `_app_out`
  and is never stored. The stored status keeps its CHECK.
- No other read changed. `_require_app` still skips every deleted row, so a
  `deleting` app is 404 on open, card, menu, remix, share, revert, rename and
  a new session, for its owner too. Discover and the builder counts skip it.
  Share and menu rows went in step 1, so no viewer's query can reach it.
- The owner's clients learn of both moments from the same frame and re-read
  the list: after step 1 the row is `deleting`, after step 5 it is absent.

### What stops answering, and when
- After step 1: `_require_app` filters `deleted_at`, so open, card, menu,
  remix, share, revert, rename and a new session all answer 404. Both lists
  and the builder counts filter it. No grant can be minted.
- `create_session` re-checks inside its own transaction, and the publish
  claim now requires an open session, so neither can start on an app the
  delete has just marked.
- Sessions are closed, so the broker's pre-flight (`harness-view`) reads
  `open: false`, and the stage endpoint answers 410, then 404 once the chat
  is gone.

### Grants already minted
- A grant is stateless and lives 12 hours. The gate has no database and
  cannot be told. What stops it is the web root not being there:
  `gate._read` answers 404 when `<www>/<app>/<root>` is not a directory.
- Window on a successful delete: from the transaction's commit to the seat
  removing the web root, which is the time a transient unit takes to start.
  Success is returned only after the helper exits 0.
- After success: nothing is served. What remains is in browsers: a tab that
  already loaded the page, and non-HTML files in that browser's private cache
  for up to 60 seconds (`Cache-Control: private, max-age=60`).
- If the helper fails before the web root goes (not installed, unit did not
  start, the name holds a file or another owner's directory): the response
  is an error, not success, and earlier grants work until they expire or the
  delete is finished. The dialog says this, and the `deleting` row stays.
- A symlink at the web root no longer keeps anything served: the seat
  unlinks the link and goes on.

### Why a running turn refuses instead of being stopped
`end` records a stop and closes the session in one transaction; the unit is
stopped later by the broker's reaper, and takes up to its stop timeout. A
delete cannot be that one step:
- the turn's harvest writes the work tree, the git dir and the preview root
  on its way out, after the delete would have removed them;
- the reaper finds the stop by reading the session row, which the chat's
  cascade removes;
- the turn's record would have nowhere to land.
So the delete reuses the gate's pieces (`_running_turn`, `_close_session`)
and refuses while a turn is live, the way Live already does.

The remaining race is a turn the broker has spawned and not yet reported.
The helper's seat half catches it: after removing the web root it asks
systemd for a running turn of this app and refuses (64, shown as 409). The
app is then deleted in the database, its pages are gone, and the other trees
wait for Finish deleting.

If systemd cannot answer, the delete stops at the same place with the same
64: "cannot tell whether a build of <id> is still running (…); its pages are
gone, finish deleting it when no build is running". Publish reads a failed
query as "no turn", because there a wrong yes is a Live button that never
works. Here a wrong no removes a work tree and a git dir under a running
turn, and a wrong yes costs one more click.

### The helper's `delete` mode (builder lane)
`harness/cc/apps/disjorn-apps-launch`, `delete_app` and `main_op`.
- **Root's half**: `check_app_id`, exactly one argument, `check_wrapper` on
  itself, then `build_op_argv`: the same argv as publish with
  `delete-as-seat` and unit `disjorn-apps-delete-<id>`. No pre-flight reads
  a file. Two deletes of one app collide on the unit name.
- **The seat's half** (`delete-as-seat`, refuses to run as root, named by no
  sudoers line):
  1. remove `<www>/<id>`;
  2. ask systemd for a running turn of the app (`active_turn_for_app` with
     `strict=True`). A failed `list-units`, a failed `show`, an exception or
     a line that is not one of our units raises `TurnQueryFailed`;
  3. only on a clean "none": remove `<www>/.staging/<id>`, `<apps>/<id>`,
     `<git>/<id>.git`, and each matching turn dir, then `rmdir` a session dir
     left empty. Otherwise exit 64.
- **What "remove" means**: `lstat` the name.
  - Absent is fine.
  - A symlink is unlinked. The link goes, its target is not opened.
  - A real directory owned by the seat is removed by `_rmtree_own`.
  - Anything else (a file, another owner's directory) is left exactly as it
    is and the run exits 1 naming it, after the other names are done.
- **`_rmtree_own`**: `shutil.rmtree`, which never follows a link inside the
  tree, with an `onexc` handler (`onerror` under Python older than 3.12; the
  house runs 3.13). On a `PermissionError` it adds the owner's rwx bits to
  the failing path and its parent, when each is a real directory by `lstat`
  and lies at or below the tree's top, then removes that entry again. A
  second failure on the same path is raised. A directory the seat does not
  own cannot be chmodded by the seat, so that still ends in exit 1.
- **Idempotent**: a second run finds nothing and exits 0.
- **Turn dirs, and why no session ids are passed**: turn dirs are keyed
  `<session>/<turn>` and carry no app id in their path. The seat finds them
  by what they are: a turn dir whose `result.json` (the harvest's record) or
  `.harvest-input.json` (the wrapper's, left when a turn died early) names
  this app. Both are written host-side by the seat; the turn dir is not
  mounted into the container. Passing session ids from the server was the
  other shape. It was not taken because it makes the alias variable-length,
  lets the caller name sessions the helper cannot check, and forces the
  server to keep session rows alive until the files are gone.
  - Cost: every turn dir's record is read on each delete.
  - Not removed: a turn dir with neither record, from a turn killed before
    its harvest spec was written. It holds a 0600 prompt and logs readable
    by the seat alone.
- **Never touched**: `/srv/apps-quarantine/<id>`. Quarantined files are
  evidence of a credential write and stay for the admin.
- `active_turn_for_app`'s two warnings now say "going on without that
  check" instead of "publishing without that check". Publish's behaviour is
  unchanged.
- **A revert racing the delete** is possible and is not claimed against.
  Revert takes no session claim. If its three renames overlap the delete's
  `rmtree`, one of the two fails: the revert answers 502, or the delete does
  and the row stays `deleting` for a retry. The losing revert can leave its
  swap tree under `<www>/.staging/<id>`, which is not served; a second
  delete removes it.

### Server (custodian lane)
- `server/app/migrations/025_apps_delete.sql`: `deleted_at`,
  `deleted_builds`, `purged_at`. 025 was the next free number; another branch in flight
  may want it and the orchestrator renumbers at merge.
- `server/app/routers/apps.py`: `DELETE /apps/{app_id}`,
  `_mark_app_deleted`; the filters and the two in-transaction guards above;
  the owner's `deleting` rows in `list_apps`.
- `server/app/models.py`: `AppStatus` gains `deleting`.
- `server/app/routers/channels.py`: `_destroy_channel` renamed
  `destroy_channel`, because a second router now calls it.
- `server/app/ws.py`: the `app_delete` fan-out, one `_send_to_user` per
  recipient.
- **Status codes**: 404 unknown or malformed id; 403 a non-owner of a
  living app, the sentence the other owner verbs use; 404 a non-owner of a
  deleted app; 401 a bot; 400 the platform's own `disjornrepo2` row, whose
  repo sessions borrow real channels; 409 a running turn, a publish in
  flight, or the helper's refusal (a turn running, or systemd not saying);
  502 the helper failed.
- **The meter**: the daily count reads `app_sessions`, and those rows go
  with the chat. So the transaction stores, in `deleted_builds`, how many of
  the app's sessions started today had spent a turn, and `_quota_for` adds
  that for apps deleted today and stops counting a deleted app's surviving
  rows. A deleted app still costs today what it cost. An open session with
  no turn stops counting, as it does when ended.
- **Remixes**: lineage is `parent_app_id` and one empty commit in the
  child's own git dir. `remix_app` fetches the parent's published commit into
  an empty repository, or snapshots its live tree, and nothing afterwards
  reads the parent. A child of a deleted parent keeps its row, card,
  session, Keep building, Live and Remix. Its `parent_app_id` names a
  tombstone, whose card is 404.

### Client (custodian lane)
- `lib/appRow.ts`: `canDeleteApp`, `deleteReachLine`, and the `finish`
  action for a `deleting` row.
- `components/DeleteAppModal.tsx`: the dialog. It holds its own copy of the
  app, because the row turns `deleting` once the server deletes it.
- `stores/apps.ts`: `deleteApp`, `onAppDelete`, `deletedAppIds`. On the
  frame the owner's row is greyed at once and the list is re-read.
- `views/AppShell.tsx`: the row's `⋯`, the card's button, the dialog, the
  `deleting` row and its Finish deleting, and closing a build modal whose
  app was deleted.
- `api.ts`, `types.ts`, `ws.ts`, `app.css`: the call, the frame, the case,
  the styles.

## Verification
**By test**
- `server/tests/test_apps_delete.py` (14):
  - owner only: 403 for a shared viewer and a stranger, 401 for the builder
    and the broker, 404 for an unknown id;
  - the platform row is refused and its channel survives;
  - a deleted app is 404 on ten verbs for the owner, a viewer and a
    stranger, absent from both lists and the builder counts, its share rows
    gone, and `app_delete` names the owner and the viewer only;
  - the real gate, with the launcher's own `delete_app` on scratch roots:
    grants minted before the delete, for the live root and the preview root,
    answer 404 after it;
  - a running turn and a stopping turn refuse; an idle open session is
    closed;
  - a publish in flight refuses; a delete that lands inside a publish or
    inside `create_session` wins;
  - a failed helper leaves a deleted, unpurged app: listed to its owner
    alone as `deleting`, not to a menu holder or an admin, 404 on the ten
    other verbs; a refusal keeps it listed; the next call purges it and it
    is gone; one more call is still 200;
  - the real gate again: a grant that served through a symlinked web root
    answers 404 after the delete, and the link's target is intact;
  - each build chat's `channel_deletions` row and `channel_delete` event;
  - a remix and a remix of the remix after their parents are deleted;
  - the meter, including the failed-helper state;
  - the frames on real sockets: owner and menu holder, then the owner
    alone at the purge, never a stranger;
  - migration 025 through `run_migrations`.
- `harness/cc/tests/test_apps_delete.py` (65):
  - root's argv byte for byte, and root removes nothing;
  - thirteen bad ids and six wrong argument lists refused by both halves;
  - the alias's regex: one argument string admitted, eight refused;
    `visudo -cf`;
  - exactly one app's paths removed, a sibling app, an unrecorded turn dir,
    the staging root and the quarantine untouched;
  - idempotent; a symlink at each of the four names and at each served
    root is unlinked with its target untouched; a symlinked session and turn
    dir is skipped; another owner and a file at the name are left, exit 1;
  - a running turn of this app and of another;
  - a turn query that fails four ways (non-zero exit, a line that is no
    unit, a failed `show`, no `systemctl`): only the web root goes, exit 64;
    publish still reads the same failure as no turn;
  - a tree with 0555 and 0000 directories and a read-only file is removed,
    and a file and a 0555 directory outside it, linked from inside, keep
    their modes and content;
  - the seat half as a subprocess;
  - a record written by the real harvest is found.
- `harness/cc/tests/test_apps_launch.py`: the alias pin names six.
- `client/tests/app_row.test.mjs`: who is offered Delete, what the dialog
  says about reach, and that a `deleting` row offers its owner `finish` and
  nothing else.

**Not run**
- The helper through real `sudo` and `systemd-run`. Every test runs it
  under the dry-run switch or calls the seat's function directly.
- `systemctl list-units` from inside the transient unit as
  `res-appsbuilding`. It is the first systemctl call the seat makes there.
  The same query answers for an unprivileged user on this box. If it fails
  for the seat, every delete stops after the web root with the "cannot
  tell" sentence and the row stays `deleting`. Manual check 2 is where that
  shows.
- Anything in a browser. The client was typechecked and built.

## Manual checks before merge
1. `sudo -l -U plink` lists `DISJORN_APPS_DELETE` after the install, and
   `sudo -n /usr/local/lib/disjorn/disjorn-apps-launch delete <id> 1`
   is refused by sudo.
2. Delete a stale draft remix: `⋯`, Delete app…, Delete app. The row goes;
   `/srv/apps/<id>`, `/srv/apps-git/<id>.git`, `/srv/apps-www/<id>` and its
   turn dirs are gone; `GET /channels/deletions` shows its build chats. If
   the answer is "cannot tell whether a build … is still running", the seat
   cannot query systemd from its unit and no delete will finish until that
   is fixed.
3. Delete a live shared app with its page open in another browser: a reload
   there answers 404.
4. A second device's list drops the row without a reload.
5. Delete a parent whose remix is live: the remix opens, builds and remixes.
6. Start a turn, press Delete: the 409 sentence, nothing removed.
7. Phone: the `⋯` is visible and the dialog fits.
8. Finish deleting: move `/usr/local/lib/disjorn/disjorn-apps-launch` aside,
   delete an app (502), close the dialog and reload. The row is greyed with
   Finish deleting, on a second device too. Put the launcher back and click
   it: the row goes everywhere.

## Follow-ups (not built)
1. **Bulk cleanup.** Multi-select delete in the Apps list, and "delete all
   my never-published remixes". An idea only.
2. **Drift line.** `10-appsbuilding.sh --drift` should list app directories
   on disk with no living row. Not added: that block reads no database
   today, and opening the house's WAL database as root can leave root-owned
   side files beside it.
3. A failed remix leaves a clone the house could not remove. It now can,
   with `delete <child>` in `remix_app`'s rollback.
4. A sweep for turn dirs that carry no record.
5. A turn whose terminal event never arrived, or a publish claim left by a
   killed process, blocks the delete until an admin clears it. The same
   stuck session already blocks Live.
6. The platform's `disjornrepo2` row shows in its owner's Apps list and
   offers a Delete the server refuses. The client has no way to know that
   id.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian and builder (cross-lane, see the split).
- **Review owner**: Claudette for the custodian surfaces, Gable for the
  builder surfaces.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - custodian: `server/app/routers/apps.py`, `server/app/routers/channels.py`,
    `server/app/ws.py`, `server/app/migrations/025_apps_delete.sql`,
    `server/tests/test_apps_delete.py`, `server/tests/test_client_app_row.py`,
    `client/` → review owner Claudette
  - builder: `harness/cc/apps/disjorn-apps-launch`,
    `harness/keyboard/92-disjorn-apps.sudoers`,
    `harness/cc/tests/test_apps_delete.py`,
    `harness/cc/tests/test_apps_launch.py` → review owner Gable
  - `harness/keyboard/10-appsbuilding.sh` is not changed. It already
    installs and drift-checks both builder files.
  - `harness/prose-baseline.toml`: lines lowered, never raised, in both
    lanes.
- **Split agreed in #custodian**: pending.

## Answers from review
1. **Owner only.** Confirmed. No admin override.
2. **409 during a running turn.** Confirmed. Delete does not stop a turn.
3. **No who-deleted record on the tombstone.** Only the owner can delete, and
   `channel_deletions` names them once per build chat. A column is added the
   day an admin override exists.
4. **A symlinked web root.** Changed after review: the link is unlinked and
   the delete goes on. A file or another owner's directory still stops it.
5. **A half-finished delete.** Changed after review: `purged_at`, the
   owner's `deleting` row and Finish deleting.
6. **A failed turn query.** Changed after review: it stops the delete after
   the web root.
7. **A closed directory in a tree.** Changed after review: `_rmtree_own`.
8. **A revert racing the delete.** Noted, not claimed against: it ends in a
   502 on one side and a retry.

## Question still open
1. Turn dirs are found by their own record, and one with no record is left
   behind. Acceptable, or should the server pass session ids?

## Expected diff tier
Tier 2: a new sudoers alias, a destructive mode in a root-installed helper, a
migration and a new endpoint.

## Token estimate
One keyboard session, spent. The branch is built.

## Deploy recipe
Order matters: the helper and the alias go in before the server that calls
them. A server restarted first deletes the app and answers 502 because sudo
refuses the mode; the row shows Finish deleting, which succeeds once the
alias is installed.
1. Merge.
2. Back up the DB (`sqlite3 .backup`).
3. At the keyboard, from the production tree:
   ```
   sudo install -o root -g root -m 0755 harness/cc/apps/disjorn-apps-launch /usr/local/lib/disjorn/disjorn-apps-launch
   sudo visudo -cf harness/keyboard/92-disjorn-apps.sudoers
   sudo install -o root -g root -m 0440 harness/keyboard/92-disjorn-apps.sudoers /etc/sudoers.d/92-disjorn-apps
   sudo bash harness/keyboard/10-appsbuilding.sh --drift
   ```
   The drift block must print `same` for both files. A full
   `sudo bash harness/keyboard/10-appsbuilding.sh` does the same installs.
4. `sudo systemctl restart disjorn`, which applies 025.
5. `cd client && npm run build`.
6. The gate and the broker are not restarted. Neither reads anything this
   build changed.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 3576
- **Confirmed at**: 2026-11-11
- **Reviews**: custodian half PASS #3573, gable half PASS #3574, both at 2cc45df; their four folds are the commits after it.
<!-- No Confirm record → no build. This is the gate. -->

## Status
merged
<!-- advanced from `built@loop/2026-11-11-apps-delete` by `board --mark-merged` on 2026-11-12: build merged as 87f1840. The word `built@loop/2026-11-11-apps-delete` on a merged spec made it indistinguishable from a buildable one. -->
<!-- One of: draft / confirmed / building / built@<branch> / merged / failed. -->
