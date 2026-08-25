# Spec: Plan Room Phase II slice A — /backlog lifecycle verbs, `duplicate` status, reject button

## Request
- **Verbatim**: "every non-chat action that I need to take currently can be done via the UI" (item 5 of the Phase II list); "`/backlog reject` needs a button, too"
- **Requester**: plink
- **Origin**: #custodian seq 1604 (Phase II kickoff), rulings seq 1609; carried items from seqs 1589/1592/1596 (`duplicate` value, reject|spec'd|built verbs)

## Agreed UX
- `/backlog reject|duplicate|spec'd|built <id>` — subcommands on the
  existing `/backlog` command (`spec'd` additionally takes the spec
  **slug**, not a path: `/backlog spec'd 6 2026-08-23-db-write-lock` —
  Claudette #1619: a card's identity is already its spec slug, the path
  is derivable, and a stored path breaks the day `SPECS/` is
  reorganized; see `spec_ref` note below). Same
  authenticated-human guard as `/build`; bots refused server-side. The row's status changes; the card leaves or
  moves the board at the next 900s derivation tick, announced by the broker
  like every other transition — the command edits the artifact and the
  machinery says so.
- New status value `duplicate` — the honest word for a row filed twice by UI
  error. Row 3 took `rejected` on 08-23 for want of it (Claudette's
  lossiness flag, seq 1592). plink named three verbs; `duplicate` ships
  alongside because a status value with no writer is dead vocabulary.
- **Reject button** on Backlog-column cards (plink seq 1609). The button
  calls the same server path as the slash command — one write path, two
  callers. A greyed button is UX; a refused call is enforcement. The gate
  lives server-side and the column merely reflects it.
- Every status change records who and when as data columns, not prose —
  backlog #5's principle applied to this new write path from day one.
  Attribution is typed, not a label: `status_by_type` / `status_by_id`,
  the same shape `messages` uses (Claudette #1615 — `backlog.author` is
  already prose; this migration must not add a second prose channel while
  the table is open).

## Architecture notes
- `server/app/migrations/` — new migration: backlog table rebuild (SQLite
  CHECK change requires it; backup-before-migration rule applies) widening
  the CHECK at `006_backlog.sql:19` to
  `(open | spec'd | built | rejected | duplicate)`, plus nullable
  `status_by_type` / `status_by_id` / `status_at` columns (typed
  attribution, same shape as `messages`; not prose TEXT), backfilled NULL.
- `server/app/models.py:19` — `BacklogStatus` literal gains `duplicate`.
- `server/app/routers/slash.py` — subcommand parse on the existing
  `/backlog` handler; human guard same shape as `/build`; the write goes
  through `db.py`'s transaction path (post-#6 lock).
- `server/app/routers/planroom.py` — derivation untouched (it already cards
  `status='open'` rows only). One POST endpoint for the button, calling the
  same status-change function as the slash path.
- A `spec'd` write sets `spec_ref` in the same statement (Claudette #1615)
  — the status implies the pointer; shipping the verb without the field
  leaves a status with no referent. `spec_ref` stores the **slug**;
  resolution to a path happens at read time (Claudette #1619 — same
  reasoning as not minting a second card ID).
- `client/` — reject button on backlog cards; the greyed render mirrors the
  server gate, never replaces it.
- Board owns no state: for backlog cards the DB row is the artifact; the
  button writes through to it.

## Preconditions (hard)
- Backlog #6 (`db.py` write lock, spec 2026-08-23-db-write-lock.md) merged
  and deployed first. Buttons multiply concurrent writes, and today's shared
  connection can silently commit another handler's half-finished
  transaction — the duplicate rows were the mild symptom. No write-through
  UI ships onto the unlocked connection.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — `server/app/` + `client/` are Claudette's surface.
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: Gable (plink, seq 1604).

## Expected diff tier
Tier 2 — prod table migration plus a human-auth surface. Advisory; the
classifier gates the actual result at merge.

## Token estimate
Well under one slot; the slack goes to tests (bot refused at the guard,
status write + tick transition, migration round-trip on a copied db).

## Phase II remaining — declared, deliberately NOT in this spec
Slice B (spec lifecycle: draft→confirmed with seq capture, mirror→main
move), slice C (diff view + review stamps), slice D (merge/deploy buttons,
H13-D3-grade scrutiny — that button is a hand on prod), item 1 (card-ID
render + #custodian card links), item 3 (draggable modal — plink's lane;
cards never draggable), item 4 (subject-system tags in the artifact:
backlog column + spec frontmatter; tags say what the work is about, per
seq 1609 — attribution stays in structured fields). Each ripens into its
own spec — signpost, not parking space; merged specs are un-pressable by
construction (Claudette, seq 1374). Item 2 (bot drafting on
heartbeat/ripe-tag) dropped per seq 1609, revisited in its own session
after D.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 1625
- **Confirmed at**: 2026-08-23
<!-- No Confirm record → no build. This is the gate. -->

## Status
merged
<!-- advanced from `built@loop/2026-08-23-plan-room-phase2-slice-a` by `board --mark-merged` on 2026-08-25: build merged as 8b61d04. The word `built@loop/2026-08-23-plan-room-phase2-slice-a` on a merged spec made it indistinguishable from a buildable one. -->
<!-- set by the broker on 2026-08-23 23:57Z (start-build, 2026-08-23-plan-room-phase2-slice-a): build published: disjorn.git 9b3f6796a7ad60fad32196cbfe3d2e54e9b04409 — on the branch for review, nothing merged. `board --mark-merged` advances this to `merged` once the merge lands. -->
<!-- set by the broker on 2026-08-23 23:41Z (start-build, 2026-08-23-plan-room-phase2-slice-a): build running as disjorn-build-2026-08-23-plan-room-phase2-slice-a.service -> loop/2026-08-23-plan-room-phase2-slice-a, launched by gable (confirmed by plink, #custodian seq 1625). Not buildable again until this line moves. -->
