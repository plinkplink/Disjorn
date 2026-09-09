# Spec: APPS tab — user stop of a running turn (builder-seat slice (iv))

Child of `2026-09-06-apps-builder-seat.md`. Amends §E.1 check 3 (the "no
UI may promise a mid-turn stop" clause), §E harvest, §H, §J. Supersedes
the parent's mid-turn-stop clause and the flow draft's "promise no
mid-turn stop" sentence.

## Request
- **Verbatim**: "your prompt wording around stopping the build raises the
  question, should we be able to stop it? I think we should and users
  will expect this. With a confirm dialog. Gable how difficult do you
  estimate this feature is to add and should it go in slice iii?"
- **Requester**: plink
- **Origin**: #custodian seq 2392; estimate and shape in Gable #2393;
  "let's spec that" #2395.

## Agreed UX
- The build modal shows **Stop** while a turn is running (stage between
  `scoped` and the turn's terminal event). Idle sessions show no button.
- Click opens a confirm dialog. Its words are the promise, exactly: "Stop
  this turn? The builder gets a moment to save what it has. Files written
  so far stay in the project; the preview does not change. This ends the
  turn, not the session. Takes up to about a minute and a half."
- Confirm posts the stop. The button reads "Stopping…" and is disabled
  until the turn's terminal system line lands. No second click, no cancel
  of a stop.
- The room's system line for the turn reads `Turn N halted — stopped by
  the user.` followed by the files sentence and the builder's quoted words
  the same way other halts do (§H).
- **End session while a turn runs** shows the same dialog with one more
  sentence ("Ending the session stops the turn first.") and does both.
- Tokens spent up to the stop count toward the session ceiling.
- Nothing promises "instant". The hard bound is the unit's stop timeout.

## Architecture notes
Wire, in order of the click:

1. **Server** (`server/app/routers/apps.py`, migration 013):
   - `app_sessions.stop_requested_at TEXT NULL`.
   - `POST /apps/sessions/{id}/stop`, owner-only, same auth as `end`.
     Sets `stop_requested_at` if NULL and the session is open; idempotent;
     410 on an ended session; 409 if no turn is running (no `scoped` since
     the last terminal event). Returns `{id, stop_requested_at}`.
   - `POST /apps/sessions/{id}/end` sets `stop_requested_at` too when a
     turn is running, before `ended_at`. Finding (#2393): today `end` only
     sets `ended_at`, the unit runs to `turn_max`, and its record 410s.
   - `harness-view` gains `stop_requested_at`.
   - `HaltReason` gains `"stopped"`; `HALT_SENTENCES["stopped"] =
     "stopped by the user"`. The set stays closed.
   - Stage post: the publisher's terminal event with `detail.halted =
     "stopped"` is accepted after `ended_at` is set **only for the turn
     that was running when `end` was called** (the record of the stop
     must land; nothing else gets through the 410). Reset
     `stop_requested_at` to NULL on any terminal event. Residue, bounded
     and recorded (Claudette #2447): if that turn's terminal event never
     arrives — harvest dies before `result.json` AND the broker's
     synthesized halt fails to post — `stop_requested_at` stays set on the
     ended session and that ONE turn number keeps its 410 exemption. It is
     keyed on the turn, so nothing else can use it; the exemption admits
     one terminal event and spends itself.
   - `stop` 410s on `ended_at` only, never on a lapsed lock (#2447): the
     lock is chat exclusivity, and the owner back from a lapse is the one
     most likely to be looking at a runaway turn.
2. **Broker** (`brokerd.py`, apps reaper): each poll already reads the
   result dir; add one `harness-view` read per poll (the reader exists
   for check 1). If `stop_requested_at` is set and the unit is alive,
   call the launcher's `stop` once, mark the ticket `stop_sent`, then
   fall into the existing wait-on-live-unit path bounded by
   `unit_stop_timeout_sec`. The reaper does not synthesize a stop record;
   the harvest writes it. If the unit is already gone, do nothing; the
   harvest lands on its own.
   Config: `[apps].stop_poll_sec`, default 5 (poll cadence while a turn
   runs; today's cadence if higher).
3. **Launcher** (`harness/cc/apps/disjorn-apps-launch`): new subcommand
   `stop <principal> <session> <turn>`, fixed argv, same charset checks
   as `run`, execs `systemctl stop disjorn-apps-<session>-<turn>`. Before
   exec it drops a marker `stop-requested` in the turn's result dir so the
   harvest can tell a stop from the clock. Today only `run` exists; the
   keyboard README says an early death is `sudo systemctl stop` by hand.
   Sudoers: one line in `harness/keyboard/92-disjorn-apps.sudoers`
   matching `disjorn-apps-launch stop` with the same argument regexes as
   `run`. Keyboard install via `10-appsbuilding.sh` (visudo -c first, as
   already written).
4. **Unit** (`run-apps.sh`): the TERM trap already ends the turn and
   harvests. No change. `TimeoutStopSec` = `unit_stop_timeout_sec` (90)
   is the hard bound after `systemctl stop`.
5. **Harvest** (`apps_harvest.py`): exit in `TIMEOUT_EXIT_CODES` with the
   `stop-requested` marker present maps to `halted = "stopped"`, else
   `"timeout"` as today. Everything else unchanged: commit `turn N
   (halted)`, preview untouched, usage recorded, files named.
6. **Client** (`AppBuildModal.tsx`, `stores/apps.ts`, `api.ts`): button,
   dialog, `stopping` state cleared by the turn's terminal event; `end`
   while running routes through the same dialog. Red chip renders the
   `stopped` reason with the same halted styling.

Semantics ruled here (Gable #2393, no objection at #2395):
- Stop ends the **turn**, never the session. The next turn sees the
  committed tree.
- A stopped turn's tokens count. Ceiling check 3 is unchanged.
- One stop per turn; a second click is a no-op server-side.
- Flow draft sentence "Promise no mid-turn stop" becomes: "The user can
  stop a turn from the modal; you cannot, and you do not promise how
  fast it ends." Parent §E.1 check 3 line 246 reads instead: "the UI may
  offer a stop that ends the turn within the unit's stop timeout; it is
  not a budget."

Tests (broker suite, harness/cc suite, server suite):
- stop while running: unit receives `systemctl stop`, harvest lands
  `halted: "stopped"`, commit present, preview untouched, ticket gone.
- stop after the unit has exited on its own: no launcher call, harvest's
  own reason stands.
- stop past `unit_stop_timeout_sec`: existing hard-bound path, reason
  string unchanged.
- end-while-running: `stop_requested_at` then `ended_at`; the terminal
  event for that turn is accepted, the next handoff 410s.
- 409 on stop with no running turn; 410 on ended; idempotent second stop.
- harvest mapping: 143 with marker = stopped; 143 without = timeout.
- launcher argv test for `stop`, byte for byte, dry-run.

Out of scope: pausing; stopping a session's whole queue; any streaming
of builder output (its own spec, if ever); a stop from a resident seat.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — broker, `harness/cc/apps`, `harness/keyboard`,
  server, client.
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink's call; BuildGable seat on its own branch is the
  shape that fit slices (i)–(ii). Sudoers install is keyboard-only.

## Cross-lane split
- **Applies**: no.

## Expected diff tier
Tier 2: sudoers line, migration, privileged launcher subcommand. About
five files plus migration and sudoers, roughly 300 lines with tests.
Backup before migration 013.

## Token estimate
One BuildGable day; comparable to slice (ii)'s smaller half.

## Ordering
Independent of slice (iii) (no shared files). Builds in parallel; lands
before the go-live flip so the first enabled verb ships with a stop.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2444
- **Confirmed at**: 2026-09-09 01:47Z (recorded at the keyboard on his behalf)

## Status
`confirmed`
