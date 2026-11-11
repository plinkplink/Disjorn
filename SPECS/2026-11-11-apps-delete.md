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
  or late. Nothing restores a removed app: `harness/backup` does not cover
  `/srv/apps*`.
- **What it cannot reach**: anything outside `/srv/apps-www/<id>`,
  `/srv/apps-www/.staging/<id>`, `/srv/apps/<id>`, `/srv/apps-git/<id>.git`
  and the turn dirs under `/srv/apps-turns` whose own record names `<id>`.
  `/srv/apps-quarantine` is never opened.
- **Who removes**: `res-appsbuilding`, inside a transient unit, exactly as
  publish, revert and remix do. Root validates the one id, builds a fixed
  `systemd-run` argv and execs. Root removes nothing.

## Agreed UX
Proposed. Nothing below has been agreed in #custodian yet.

**Who**
- The app's owner, and nobody else. Not an admin: no owner verb in
  `routers/apps.py` has an admin override, and this build adds none
  (question 1). Never a bot.

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
  becomes **Try again**. If the server had already hidden the app, the dialog
  says so: off every list, no new link can be made, files still on disk, a
  link opened earlier may still work.

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
5. `{"ok": true}`.

A failure at 3 or 4 leaves the app deleted in the database. The owner's next
DELETE skips step 1 and runs 3 and 4 again. A deleted app answers 404 to
everyone else.

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
  start, the root is not a real directory of the seat's): the response is an
  error, not success, and earlier grants work until they expire or the
  delete is repeated. The dialog says this.

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
wait for a second DELETE.

### The helper's `delete` mode (builder lane)
`harness/cc/apps/disjorn-apps-launch`, `delete_app` and `main_op`.
- **Root's half**: `check_app_id`, exactly one argument, `check_wrapper` on
  itself, then `build_op_argv`: the same argv as publish with
  `delete-as-seat` and unit `disjorn-apps-delete-<id>`. No pre-flight reads
  a file. Two deletes of one app collide on the unit name.
- **The seat's half** (`delete-as-seat`, refuses to run as root, named by no
  sudoers line):
  1. remove `<www>/<id>`;
  2. ask systemd for a running turn of the app (`active_turn_for_app`,
     advisory as it is for publish);
  3. if none: remove `<www>/.staging/<id>`, `<apps>/<id>`, `<git>/<id>.git`,
     and each matching turn dir, then `rmdir` a session dir left empty.
- **What "remove" means**: `lstat` the name. Absent is fine. A real
  directory owned by the seat is `rmtree`d, which never follows a link
  inside it. Anything else (a symlink, a file, another owner's directory) is
  left exactly as it is and the run exits 1 naming it, after the other names
  are done.
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
  check" instead of "publishing without that check".

### Server (custodian lane)
- `server/app/migrations/025_apps_delete.sql`: `deleted_at`,
  `deleted_builds`. 025 was the next free number; another branch in flight
  may want it and the orchestrator renumbers at merge.
- `server/app/routers/apps.py`: `DELETE /apps/{app_id}`,
  `_mark_app_deleted`; the filters and the two in-transaction guards above.
- `server/app/routers/channels.py`: `_destroy_channel` renamed
  `destroy_channel`, because a second router now calls it.
- `server/app/ws.py`: the `app_delete` fan-out, one `_send_to_user` per
  recipient.
- **Status codes**: 404 unknown or malformed id; 403 a non-owner of a
  living app, the sentence the other owner verbs use; 404 a non-owner of a
  deleted app; 401 a bot; 400 the platform's own `disjornrepo2` row, whose
  repo sessions borrow real channels; 409 a running turn, a publish in
  flight, or the helper's refusal; 502 the helper failed.
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
- `lib/appRow.ts`: `canDeleteApp`, `deleteReachLine`.
- `components/DeleteAppModal.tsx`: the dialog. It holds its own copy of the
  app, because the row is gone once the server hides it.
- `stores/apps.ts`: `deleteApp`, `onAppDelete`, `deletedAppIds`.
- `views/AppShell.tsx`: the row's `⋯`, the card's button, the dialog, and
  closing a build modal whose app was deleted.
- `api.ts`, `types.ts`, `ws.ts`, `app.css`: the call, the frame, the case,
  the styles.

## Verification
**By test**
- `server/tests/test_apps_delete.py` (13):
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
  - a failed helper leaves a deleted app; the second call finishes; a third
    is still 200;
  - each build chat's `channel_deletions` row and `channel_delete` event;
  - a remix and a remix of the remix after their parents are deleted;
  - the meter, including the failed-helper state;
  - the frames on real sockets: owner and menu holder, not a stranger;
  - migration 025 through `run_migrations`.
- `harness/cc/tests/test_apps_delete.py` (55):
  - root's argv byte for byte, and root removes nothing;
  - thirteen bad ids and six wrong argument lists refused by both halves;
  - the alias's regex: one argument string admitted, eight refused;
    `visudo -cf`;
  - exactly one app's paths removed, a sibling app, an unrecorded turn dir,
    the staging root and the quarantine untouched;
  - idempotent; a symlink at each of the four names; a symlinked session
    and turn dir; another owner; a file at the name;
  - a running turn of this app and of another;
  - the seat half as a subprocess;
  - a record written by the real harvest is found.
- `harness/cc/tests/test_apps_launch.py`: the alias pin names six.
- `client/tests/app_row.test.mjs`: who is offered Delete, and what the
  dialog says about reach.

**Not run**
- The helper through real `sudo` and `systemd-run`. Every test runs it
  under the dry-run switch or calls the seat's function directly.
- `systemctl list-units` from inside the transient unit as
  `res-appsbuilding`. The same query answers for an unprivileged user on
  this box; if it fails there, the check warns and the delete goes on.
- Anything in a browser. The client was typechecked and built.

## Manual checks before merge
1. `sudo -l -U plink` lists `DISJORN_APPS_DELETE` after the install, and
   `sudo -n /usr/local/lib/disjorn/disjorn-apps-launch delete <id> 1`
   is refused by sudo.
2. Delete a stale draft remix: `⋯`, Delete app…, Delete app. The row goes;
   `/srv/apps/<id>`, `/srv/apps-git/<id>.git`, `/srv/apps-www/<id>` and its
   turn dirs are gone; `GET /channels/deletions` shows its build chats.
3. Delete a live shared app with its page open in another browser: a reload
   there answers 404.
4. A second device's list drops the row without a reload.
5. Delete a parent whose remix is live: the remix opens, builds and remixes.
6. Start a turn, press Delete: the 409 sentence, nothing removed.
7. Phone: the `⋯` is visible and the dialog fits.

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
6. A directory a build left unreadable makes `rmtree` fail; the delete
   answers 502 until it is fixed by hand.
7. The platform's `disjornrepo2` row shows in its owner's Apps list and
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
  - `harness/prose-baseline.toml`: four lines lowered, two in each lane.
- **Split agreed in #custodian**: pending.

## Questions for review
1. Owner only, or may an admin delete someone else's app, as an admin may
   delete someone else's channel?
2. Is refusing during a running turn right, or should Delete stop the turn
   and finish when it has ended?
3. Turn dirs found by their own record, with a record-less one left behind:
   acceptable, or should the server pass session ids?
4. A name that is not a real directory of the seat's is left and the run
   fails. For a symlinked web root that means earlier grants keep working
   until someone looks. Should the link itself be unlinked instead?
5. Should the tombstone record who deleted it? Today only the owner can, and
   `channel_deletions` names them once per chat.

## Expected diff tier
Tier 2: a new sudoers alias, a destructive mode in a root-installed helper, a
migration and a new endpoint.

## Token estimate
One keyboard session, spent. The branch is built.

## Deploy recipe
Order matters: the helper and the alias go in before the server that calls
them. A server restarted first hides a deleted app and answers 502 because
sudo refuses the mode; the same DELETE succeeds once the alias is installed.
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
- **Confirmed by**: pending
- **#custodian seq**: pending
- **Confirmed at**: pending
<!-- No Confirm record → no build. This is the gate. -->

## Status
`built@loop/2026-11-11-apps-delete`
<!-- One of: draft / confirmed / building / built@<branch> / merged / failed. -->
