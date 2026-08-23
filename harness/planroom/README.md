# The Plan Room — derivation service (Phase I)

`planroom.py` derives every card on the Plan Room board from the artifacts that
already exist, writes them to a SQLite index, and hands that index to three
renderers: the Disjorn server's Plan Room tab, this module's own CLI, and the
broker's `board-*` verbs. One derivation, no forked truths.

Spec: `SPECS/2026-08-20-plan-room.md` (confirmed by plink, #custodian seq 1434).

## The load-bearing rule

**The board owns no authoritative state.** Every card is a rendering of
something that already exists — a `SPECS/` file's Status line, a confirm seq, a
gatehouse branch, a backlog row, a push-log line, deploy provenance. Dragging a
card never changes reality; changing reality moves the card.

The board therefore cannot go stale relative to the mirror, because it is not a
copy of it. The **mirror** can lag, so staleness is *declared, never denied*:
the board's face carries its derivation time and the mirror head it derived
from, and every renderer prints both.

The night these specs were reviewed, four hand-written status lines outlived
their truth in a few hours — a memory index, a memory corpus, the deploy docs,
and BUILD-LOOP.md's own lanes section (seqs 1405/1414/1419/1428). Any layer
written by hand and read as fact lies eventually. Cards derive from artifacts so
the board has a rebuild path instead of a memory.

## Cache, never source

The index is a **cache, never a source: git wins every disagreement and the
index rebuilds from zero.** If the index and the repo disagree, the index is
wrong. Delete the file; the next rebuild restores it whole. There is no path in
this house by which the index teaches git anything, and there is no incremental
update path — an index that can only be updated is an index that can drift, and
drift is the entire defect class this build exists to close.

Written broker-side. Read read-only from the server.

## What the board owns natively — and only this

- comments
- card order within a column
- the blocked flag + its reason
- archived

That state is **authoritative, server-owned, and not in this cache.** It lives
in `card_meta` and `card_comments` (server migration `009_planroom.sql`), keyed
on the spec **slug**, and it survives every rebuild. If it were in the index,
"rebuild from zero" would mean "delete every comment anybody wrote."

Blocked/On Hold is a **flag with a reason, never a column** — a held card keeps
its place so everyone can see where it re-enters.

## Where it runs, and where it must not

Broker-side, and only broker-side (seq 1428 P2). Derivation needs gatehouse
access (`sudo git --git-dir`) and `brokerd` imports; the Disjorn server process
has neither and must not grow them. **The server reads only the derived index.**
If the router ever grows its own reader of `SPECS/` or the gatehouse, that is
the forked truth this module exists to prevent — a defect, not a shortcut.

## One parser for the gate's own fields

Status and confirm-record parsing come from `brokerd`'s parsers, always,
including in the index builder (seq 1428 P3). The incident, carried verbatim
from `harness/keyboard/board.py`:

> The board's first two days it read the Status word itself, saw `confirmed`,
> and reported "nothing waiting on you" while the broker's gate was refusing the
> same spec for a confirm record whose bold was one word off.

Two parsers of one file will disagree exactly when it matters. So this module
asks the gate what the gate would say, and never re-implements the answer. The
same argument is why the aggregation itself is imported from
`harness/keyboard/board.py` rather than copied, and why the tri-state deploy
badge calls Phase 0's `metrics.deploy_state()` rather than re-deriving it
(seq 1428 P6).

## Columns (ruled seq 1391 item 1)

`Backlog → Proposed → Ready → Building → Review → Merged → Archived`

| Column | Derived from |
|---|---|
| Backlog | `backlog` table, status `open`. The only column whose cards may lack a spec file; a card's exit is a spec being drafted. |
| Proposed | Status `draft`. |
| Ready | Status `confirmed` — the gate's own launch criterion. A Ready card is literally pressable; nothing else is. |
| Building | Status `building`. `failed` shows here too, flagged, until a human resets it to `confirmed` or abandons it. |
| Review | Status `built@<branch>`, PLUS auto-cards for keyboard merges pending review and for uncited `main` commits. **The drift report wearing a UI; its resting state is empty.** |
| Merged | Status `merged` / `applied-live`, carrying the tri-state deploy badge. |
| Archived | The board-native `archived` flag on merged cards (applied server-side), and — derived — Status `superseded` / `abandoned`. Rendered as a table: "everything that's done". |

A Status word the gate cannot classify lands in **Review**, flagged
`unparseable-status`. It is not dropped: dropping it is how a file disappears
from every list at once.

### Why `superseded` / `abandoned` land in Archived

The spec names the board-native flag as the way into Archived. It does not say
where a superseded spec goes, and a superseded spec is unambiguously *done*,
which is what Archived means. It is not Merged — no merge happened, and a
column that claimed one would be the board asserting something the artifacts do
not say. Recorded here as a derivation decision, not as a ruling.

## The deploy badge (ruled seq 1391, one computation per seq 1428 P6)

Computed from `metrics.deploy_state()` and from nothing else:

| Badge | Means | From `deploy_state()` |
|---|---|---|
| green | prod matches the mirror | `state == "in-sync"` |
| amber | merged, not deployed | drift, prod only `behind` |
| red | **live, not merged** | drift with prod `ahead`, or a dirty prod tree |
| unknown | the gate is not configured | `state == "unknown"` |

Red is the dangerous one and it has two shapes, both meaning code is *running*
that the mirror has never seen. That is the ship-by-not-publishing case
(Claudette, seq 1380), and it must never render as merely "behind".

## Notifications

One system line to #custodian per **column transition**, never per edit.
Residents are event-driven, so the stream is their trigger; and because it is
posted to #custodian it doubles as a witnessable seq trail of the whole
lifecycle, for free.

The previous snapshot lives in the index and nowhere else, which is what makes
the detector stateless — there is no separate memory of "what I last announced"
to fall out of step with what the board actually shows.

**A cold start announces nothing.** With no prior index there is no prior board,
so every card would read as newly `opened` and the first rebuild after any
install — or after anyone deletes the cache, which they are explicitly invited
to do — would dump the entire history of the house into #custodian as if it had
all just happened.

## When the index rebuilds

Three triggers (seq 1428 P4), so the index refreshes when `main` moves and not
only when a resident happens to call a verb:

1. **`refresh-mirror`** — the broker rebuilds after the mirror moves.
2. **A build's terminal banner** — the same path that narrates a build's
   outcome to #custodian already refreshes the mirror, so the board moves with
   the build rather than a quarter-hour later.
3. **A broker-side timer** — a daemon thread inside `brokerd`, armed whenever
   `[planroom].index` is configured.

The timer lives *in the daemon* rather than in a systemd unit on purpose. P4
asks for "a trigger nobody has to remember", and this week's install record
argues hard against trusting a hand step: a `.timer` file is one more thing that
can be committed and not installed, and a second writer racing the daemon is one
more thing that can half-write an index. Configure the index path and the
rebuild happens; that is the whole install.

## Install

The index the server reads is written by the broker, so it lives on the
broker's side of the wall:

```
sudo install -d -o disjorn-broker -g disjorn -m 0750 /var/lib/disjorn-broker
```

`broker.toml` needs a `[planroom]` block:

```toml
[planroom]
index = "/var/lib/disjorn-broker/planroom-index.db"
rebuild_on_refresh = true
timer_sec = 900
announce = true

# Review owner for a card with no spec file, by path prefix. There is no
# default map compiled into planroom.py on purpose: a lane -> owner table is
# house policy, ruled in #custodian, and a guess in a harness module is exactly
# the hand-written layer this build exists to delete.
[planroom.lane_owners]
"server/" = "Claudette"
"client/" = "Claudette"
```

The server needs `PLANROOM_INDEX` pointing at the same file (read-only). See
`server/app/config.py`.

## Usage

```
planroom.py --config /etc/disjorn-broker/broker.toml              # the board
planroom.py --config ... --json                                   # the data
planroom.py --config ... --rebuild                                # write index
planroom.py --config ... --rebuild --transitions                  # + the lines
planroom.py --config ... --read                                   # render index
```

## Tests

```
python3 -m pytest harness/planroom/tests -q
```

The file is `test_planroom_derivation.py`, not `test_planroom.py` — the server
suite already owns that basename, and pytest collects modules by basename when
several suites share one rootdir (the same reason `broker_testlib.py` is not
called `conftest.py`).

The gatehouse-backed collectors shell out to `sudo git`, so the fixtures point
them at an empty scratch shelf; those paths are `board.py`'s and are tested
there. Everything else runs against a real git repo with a real `SPECS/` — the
artifacts *are* the fixture, which is the only way to test a module whose whole
claim is that it does not have state of its own.

## Phase II is not here

The write-through controls — confirm/witness/ratify buttons posting under the
actor's own key, diff view, merge, review stamps — are a separate spec, drafted
once Phase I is live. Nothing in this directory should grow them: a spec's
Status goes `merged` when its build merges, and merged specs are un-pressable by
construction, so parking future work inside one is how delete-channel A1/A2 went
inert (Claudette, seq 1374).
