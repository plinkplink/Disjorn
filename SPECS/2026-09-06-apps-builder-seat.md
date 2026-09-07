# Spec: APPS v1 stage 2 — apps-builder seat, handoff verb, stage publisher

<!--
DRAFT AUTHORED AT THE KEYBOARD 2026-09-06 (Fable, plink's seat) as the
stage-2 cut of SPECS/2026-08-30-apps-tab-v1.md (confirmed #2211, stage 1
merged 40c08a6). It is also the "provisioning spec" that parent spec defers
to in Rounds 4–6 (asset shelf, ponytail, builder brief). Parent rulings are
not re-litigated here; where this file names a ruling it cites the round.
Round 1 (#custodian 2284 Claudette, 2286 Gable, 2288 Claudette) accepted all
five operational rulings as proposed and is FOLDED IN BELOW in place; the
"Review record" section at the end lists what changed and whose finding it
was. Gable declined ownership (#2286); the keyboard keeps the spec.
-->

## Request
- **Verbatim**: "Stage 2 = apps-builder seat image + broker `[apps]` reader +
  spawn + build-prompt handoff + stage publisher" (parent spec, Round 13),
  and plink at the keyboard, 2026-09-06: "Good idea. I'll put my first
  thoughts on these five here and reiterate in chat if necessary … let's
  give it a try."
- **Requester**: plink
- **Origin**: keyboard, relayed to #custodian (seq of the post that
  announces this draft)

## Agreed UX
Stage 1 shipped the room; stage 2 makes something happen in it.

In the modal the resident (Gable or Claudette, on their own seat with their
own memory — parent B3) talks until it has enough, then says so and hands a
build prompt to the broker. The stage bar moves: `scoped` when the handoff
is accepted, `scaffolded` when the first file lands, `files_written` when
the turn ends with a non-empty diff (filenames scroll as they appear),
`deployed` when the turn's output is copied to the preview root. `live` is
stage 3 (it is the user's explicit "done" and the only thing that serves
publicly or posts cards). Nothing renders in the preview panel until stage
3's gate exists; in stage 2 the user sees the bar move, the filenames, the
elapsed clock, and the resident's one-line report in the chat.

**Change something** is the next user message: the resident reads it,
decides whether to ask or to act, and hands off again. Each handoff is one
**builder turn** (parent Round 5), one container, same app repo. Quota is
unchanged (sessions per day, stage 1). The hidden per-build token ceiling
(parent Round 6, 10,000,000) is enforced across the session's turns; on a
trip the turn halts, the modal shows one sentence, one line goes to
#custodian.

The builder never talks to the user. If it flags something, the flag goes to
the admin in one line and the build continues (parent "Flagging").

## Architecture notes

### A. The seat contract (runner-agnostic — plink's ruling 2 in spirit)
The apps-builder is a build hand, not a persona. Its whole interface is:

- **stdin**: the build prompt (the resident's app spec, bounded).
- **`/work`** (rw): the app repo `/srv/apps/<app-id>/`, a git repo the host
  initialised at first turn. The ONLY writable path.
- **`/shelf`** (ro): the vendored asset shelf (§F).
- **`/config-image`** (image content, ro): the builder brief
  `BUILDER-BRIEF.md` (§G) and the runner's own config (for Claude Code:
  the `CLAUDE.md` the entrypoint composes from the brief plus ponytail's
  instruction file, and managed settings). NOTHING from the host credential
  directory is mounted (slice (i) review): the credential travels by name
  only, as `run-resident.sh` does it.
- **`$HOME`**: a per-turn tmpfs, discarded at exit — Claude Code needs a
  writable home for its own state (Gable #2286). `/work` stays the only
  writable path that persists, which is the sense that matters.
- **network**: the build-seat policy — pasta plus the WP-H2 nftables wall,
  so the model API is reachable and nothing else is. The seat has no broker
  socket, no gatehouse, no house memory, no platform clone, no spine (parent
  Walls/1). Zero broker verbs by construction: there is no socket to call.
- **The wall, stated (Gable #2276, Claudette #2274):** the build seat holds
  NO house credential of any kind — not a server key, not a bot key. Its
  only inbound is the handoff payload (prompt, app id, session id); its only
  outbound is the stage-event stream, relayed by the broker under the
  `broker` bot name the server already gates by config. The blast radius of
  a prompt-injected turn is one app's own source — and §E's secret scan is
  what keeps that source from carrying the seat's model key out.
- **stdout/stderr**: the runner's own stream, spooled to 0600 files like
  build seats (BL-D2); read for usage accounting and the optional `FLAG:`
  line, never for stage detection.
- **exit code**: 0 = turn ended by the runner; anything else = halted.

**Stage events are derived HOST-SIDE from the filesystem and the process,
never from runner output — and never from a watch the broker has to be
alive to hold (Gable #2295, Claudette #2299).** `scoped` on spawn;
`scaffolded` on the first create-or-modify under `/work` **outside `.git/`
and after spawn** (the repo is initialised before spawn and turn 2 starts
non-empty, so "first path appears" would fire at t=0 every turn — Claudette
#2284), detected by a watcher INSIDE the unit that writes a marker file to
the turn's result directory (§C); the broker reads markers, it does not
watch `/work`;
`files_written` on exit 0 — with `detail.files` listing the changed paths,
or `detail.no_changes = true` and an empty list when the runner answered,
refused, or decided nothing needed changing (a turn that ends clean must
end the bar's wait, not leave the user watching a clock; the five-stage
vocabulary is a stage-1 CHECK constraint, so "no changes" is a flag on
`files_written`, not a sixth value — **which makes `files_written` the
turn's TERMINAL stage, not literally "files were written"; any reader that
renders the bar label keys off `detail`, never off the stage name**
(Claudette #2293)); `deployed` after the host copies
`/work` to the preview root. A halted turn posts its LAST reached stage
again with `detail.halted` set (§E). This is the portability wall: a different
model or a non-Anthropic runner is a different image and a different
`[apps].runner_command`, and the stage bar, the ledger and the verb do not
change. The runner-specific bits (stream-json usage fields, the CLAUDE.md
convention) are confined to the image and to one usage-parser function
named per runner.

### B. Runner, model, and the `[apps]` table (broker.toml, plink-owned)
```
[apps]
runner = "claude-code"                     # selects image + usage parser
seat_bots = { res-gable = 2, res-claudette = 1 }   # seat name -> bot id (§E.2);
                                           # a bot id is NEVER a verb argument
runner_command = ["claude", "-p", "--output-format", "stream-json"]
model = "claude-opus-5"                    # plink 2026-09-06: "Opus 5 for now"
build_token_ceiling = 10000000             # parent Round 6
session_lock_ttl = 900                     # informational here; the SERVER
                                           # enforces it (parent Round 13/D3)
origin_base = "https://debian.tailca81ba.ts.net:8443"   # stage 3 reads it
prompt_max_bytes = 65536
turn_max_sec = 1800
```
`brokerd.py` gains `self.apps = config.get("apps", {})` and a reader with
defaults; today nothing reads the table (measured, stage-1 mapping).
**Boot check on `seat_bots`** (Claudette #2293): at startup the broker reads
the server's `bots` table (the `[gate].message_db` read-only handle it
already holds) and asserts each mapped id exists and its name matches the
seat's suffix case-insensitively (`res-gable` → `Gable`). A mismatch does
not stop the broker; it disables the `apps-build` verb with an audit line,
and the verb answers "apps-build is disabled: the seat map failed its boot
check" — loud and scoped, so a renumbering can never hand one resident's
session to the other. The
model is a knob, never a string in code (provenance policy: the chooser
already prints what the seat runs, and stage 2 extends that: the builder
card's `model` for a resident builder stays the RESIDENT's pin, because the
resident is who the user talks to; the apps-builder's model is printed in
the turn's system line, §H).

### C. Host identity: `res-appsbuilding` (plink: "fine by me")
A new system user in the resident-user pattern (`01-users.sh`,
`02-podman.sh`): its own subuid/subgid range, podman `keep-id`, home
`/home/res-appsbuilding` holding only podman state. Owns `/srv/apps/`
(app repos, 0750) and writes `/srv/apps-www/<app-id>/preview/` (0755,
world-readable so the stage-3 gate, a house process, can serve it). **Only
`<app-id>/preview/` is ever served; nothing else under `/srv/apps-www/` is
a path.** In-flight publishes stage under `/srv/apps-www/.staging/<app-id>/`
(0700, the seat's alone; `.staging` cannot collide with an app id), so a
half-transferred tree or a superseded copy is never inside the served set
even if stage 3 serves the app directory (Claudette #2336). Transient
units `disjorn-apps-<session>-<turn>.service` run as this user via a
`disjorn-apps-launch` sudoers-scoped launcher, the `disjorn-build-launch`
pattern. **Its argument charsets are the whole wall between a sudoers-
scoped launcher and an arbitrary path, so they are spec, not code
(Claudette #2284):** app id `^[a-z2-7]{12}$` (stage 1 D4, base32 lower);
session id `^[1-9][0-9]{0,8}$`; turn `^[1-9][0-9]{0,3}$`; resident
`^res-[a-z]{1,24}$`; prompt path must resolve, after `realpath`, under
that resident's mapped prompt directory. Anything else is exit 64 before
any privilege is used. The launcher sets `RuntimeMaxSec`, `MemoryMax`,
`LimitFSIZE`, `TasksMax`, then `run-apps.sh`.

**Everything that touches `/srv/apps` runs in the unit as
`res-appsbuilding`; the broker (which runs as `plink`,
`harness/broker/disjorn-broker.service`) only ever reads a per-turn
result directory** (Gable #2286, #2295; Claudette #2288, #2299). The
launcher, before spawn: `git init` the app repo if absent (so §E's "the
broker never writes under `/srv/apps`" is true from the first turn).
The unit, during the turn: an in-unit watcher writes `scaffolded` (a
timestamp) into the result directory on the first qualifying change. The
launcher's harvest, on exit: exit status → `git status` → secret scan →
commit → copy, then `result.json` (exit, files, `no_changes`, `halted`,
quarantine path, spool paths) written atomically, driven from the unit's
recorded exit status so it is idempotent — a broker that restarts mid-turn
adopts a finished turn instead of missing its exit. The turn's completion
is a fact the unit recorded, not a thing the broker had to be awake to
witness.

Result directory: `/srv/apps-turns/<session-id>/<turn>/`, owned
`res-appsbuilding`, directories 0755, files 0644, so the broker can read
and inotify it without owning it; nothing else is ever written there, and
it is pruned by the broker's ledger step after the turn is recorded. New keyboard script
`harness/keyboard/10-appsbuilding.sh` creates all of it idempotently and
carries a drift test (parent "Seat image": "learn from the build-seat
provisioning gaps").

### D. Credential (plink: "I'll go grab the anthropic key")
A dedicated metered key for this seat, so app-build spend is its own line
on the bill and can be revoked without touching a resident.
**Where to drop it:** `/srv/disjorn-build-config/appsbuilding/env`, mode
0640 `root:res-appsbuilding`, one line `ANTHROPIC_API_KEY=…`. `run-apps.sh`
passes it by name via `--env-file /dev/fd/N` and masks `/config/env` with
`/dev/null`, exactly as `run-resident.sh` does, so the session never reads
the file. The keyboard creates the directory in §C's script; plink writes
the file. `RESIDENT_METERED_OK=1` semantics carry over (this seat is
metered deliberately). **Containment is that this key is dedicated,
metered, and rotatable** — that is the wall against exfiltration; §E's
scan is defense in depth (Claudette #2288). If a per-user credential slot
ever lands behind this pipeline, that sentence is the one to re-read.

### E. The handoff verb: `apps-build` (plink: "elegant … extended deliberation")
One new broker verb, enabled per resident in `verbs.toml`
(`[res-gable] apps-build = true`, `[res-claudette] apps-build = true`),
declared in `verb_surface.toml` for both seats (CLI + tool), handler in
`brokerd.py`'s static table, generator re-run, `test_verb_surface.py` green.

Args: `{"session_id": int, "prompt_file": str}` — and nothing else; in
particular no bot id. The prompt is a FILE on the caller's own volume,
resolved through that resident's `path_map`, bounded by
`prompt_max_bytes`, and refused if it carries `[[CHAT]]` markers with a
DISTINGUISHABLE sentence — "The prompt file contains a chat marker the
harness cannot pass through; quote the user's words without it" — so a
resident that faithfully quoted a user who typed the marker can say what
happened instead of "something went wrong" (Claudette #2284). Chat is
data; the prompt is data the broker reads, never argv a resident composes.

Broker checks, in order, each a flat-sentence refusal in the audit log:
1. session exists and is OPEN (`ended_at IS NULL`) — read through a new
   broker-only server endpoint `GET /apps/sessions/{id}/harness-view`
   (bot actor whose name is in `APPS_STAGE_PUBLISHER_BOT_NAMES`, i.e. the
   `broker` bot; returns app id, owner id, builder bot id, stage, turn count,
   tokens so far).
   **Slice (ii) amendment (keyboard D-A1, Claudette #2345/#2349, Gable
   #2347): a LAPSED LOCK does not refuse a handoff, and a publisher's stage
   post is not 410'd on a lapsed lock either.** The lock is the USER's chat
   exclusivity (the modal's heartbeat, ~15 min), and a turn can run longer
   than that; a resident handing off seconds after the modal closed, and the
   record of a turn that ran, both have to land. Only an explicit `end`
   (or a secret halt, which ends the session server-side) closes the door,
   at 410. This supersedes this list's earlier "lock not lapsed" and the
   parent v1 D6 where it read a lapsed lock as a refusal. The concurrent-
   writer hole a lapsed lock opens (a second session on the same app) is
   closed instead by the app-keyed one-turn claim in §E.4, NOT by the lock;
   an app's turn HISTORY is still not single-threaded (two sessions can
   alternate handoffs between turns), which is a known, named limit.
2. the caller's SEAT, as the kernel asserts it (SO_PEERCRED → `[uids]`),
   maps through `[apps].seat_bots` to a bot id equal to the session's
   `builder_bot_id`. An unmapped seat is refused loudly. The seat that
   elicited is the only seat that may hand off, and the seat never asserts
   who it is — the broker knows (Claudette #2284: without the map, check 2
   was a formality);
3. ceiling: `tokens_used < build_token_ceiling` **before** the turn.
   Usage lands when the runner finishes, so the ceiling is checked before
   a turn and recorded after: a trip blocks the NEXT handoff, and one turn
   can overshoot. That is a runaway kill under parent Round 6, not a
   budget, and no UI may promise a mid-turn stop (Claudette #2284);
4. one turn at a time per session (in-memory claim, sidecar JSON like
   builds, adopted on restart).
Then: post `scoped`; spawn the turn through the launcher (which inits the
repo if absent); post `scaffolded` when its marker appears in the result
directory; on `result.json`, post the terminal stages below. The
launcher's harvest, on exit:
- **non-zero exit** (timeout, error): commit the tree as `turn N (halted)`
  so the next turn can see the work, leave the preview root UNTOUCHED — a
  half-built app must never replace a preview that worked (Claudette
  #2284) — and report `halted = "timeout" | "error"`;
- **exit 0, empty diff**: report `files_written` with `no_changes = true`;
  nothing committed, nothing copied;
- **exit 0, non-empty diff**: **secret scan** the diff for the seat's key
  value, its base64 and its hex encodings (Gable #2286 BLOCK, Claudette
  #2288); a hit is `halted = "secret"`, NO commit (the value must not enter
  history), NO copy, and — because the brief tells the next turn to read
  `/work` first, which would read the injection back in (Claudette #2293)
  — the dirty files are moved to `/srv/apps-quarantine/<app-id>/<turn>/`
  (never mounted into any seat), `/work` is reset to `HEAD`, the SESSION is
  ended by the stage endpoint (lock released, system line "Turn N halted —
  the build tried to write a credential; this session is closed and an
  admin has been notified"), and one `FLAG` line goes to #custodian with
  the session id and the quarantine path. A turn that tried to publish a
  credential has earned a human before the next one; otherwise commit as
  `turn N`, copy to the preview root EXCLUDING `.git/` and every dotfile at
  the repo root (`.env` is the file a model writes a key into by habit),
  report `files_written` then `deployed`. The copy is PUBLISHED BY RENAME:
  rsync into a fresh sibling with symlinks and specials dropped as a class
  and the §C modes applied by rsync, then the sibling takes the preview's
  name — a copy that fails leaves the preview that worked exactly as it
  was, and no symlink ever reaches the served root (slice (i) review
  round 3, Claudette #2325/#2329, Gable #2327);
- **the harvest itself failing** (git or rsync broken partway) is a
  TERMINATED turn, not a claimed success and not a missing record:
  `halted = "error"` with an `error` string and whatever `commit` was
  already made (Gable #2327, Claudette #2329);
- **a unit that ends with no `result.json` is a halt**, timed out or not.
  The broker synthesizes the record (`halted = "error"`, detail "the turn
  ended without a result") from the unit's exit rather than leaving a
  stage bar waiting on a file that will never appear — absence has to mean
  something or it means "wait forever" (Claudette #2329). Slice (ii) owns
  this branch; the harvest makes it reachable only when `result.json`
  itself cannot be written, which the unit's journal records loudly.
The broker then parses usage from the spool, appends the ledger line, and
posts the stage events with the §H detail. The verb returns immediately
after spawn with `{turn: N, unit: …}`; the resident does not wait on it (a
summon has a clock).

**Ledger**: `/var/log/disjorn-broker/apps-ledger.jsonl`, one line per turn:
session, app, turn, unit, exit, seconds, and the usage fields the runner
reports (for Claude Code: input, output, cache_read, cache_creation), plus
`ceiling_column` naming which column the ceiling summed (parent Round 6:
"the trip log record names the column it summed"). Proposed column for v1:
`input + output + cache_creation` (fresh work; cache reads excluded) — at
10M no honest build trips on any column; revisit with data.

### F. The asset shelf (parent Round 6, Claudette's list; admission bar "an
app is worse without it")
Vendored into the image at `/shelf`, each entry with license file, pinned
version and SHA-256 in `harness/cc/shelf/MANIFEST.toml`; the builder brief
lists them with one-line usage each. Candidates (versions pinned at
vendoring, names then written back here):

| slot | package @ version (pinned 2026-09-06, npm registry) | file on the shelf | SHA-256 | license |
|---|---|---|---|---|
| 2D game lib | `kaplay@3001.0.19` | `dist/kaplay.js` (IIFE, global `kaplay`, 189 KB) | `88a946ea…ef4a4b` | MIT |
| chart lib | `chart.js@4.5.1` | `dist/chart.umd.min.js` (208 KB) | `48444a82…9f54a` | MIT |
| CSS reset | `modern-normalize@3.0.1` | `modern-normalize.css` (3.3 KB) | `b4ad31da…f0e110` | MIT |
| UI font | `@fontsource/inter@5.3.0` | `files/inter-latin-{400,500,600,700}-normal.woff2` | `8909904a…`, `f3779f1e…`, `f9a06e79…`, `6f56409f…` | OFL-1.1 |
| mono font | `@fontsource/jetbrains-mono@5.3.0` | `files/jetbrains-mono-latin-{400,700}-normal.woff2` | `14425ba9…`, `d0d4e818…` | OFL-1.1 |
| icon set | `lucide-static@1.41.0` | `sprite.svg` (500 KB, `<use href="#name">`) | `73c75bac…d0325` | ISC |
| store shim | `house-store.js` (house-written, §G) | — | at build | house |
| CC0 pack | a Kenney subset: one sprite pack, one UI pack, one sound pack | — | at vendoring | CC0 |

Full 64-hex digests live in `harness/cc/shelf/MANIFEST.toml` at build; the
prefixes above are the spec's witness. Why these: KAPLAY is one script tag
with batteries (Phaser is the fallback if it proves too thin); Chart.js is
the boring default; modern-normalize has no opinions; four Inter weights
and two JetBrains Mono weights, latin subset only; Lucide is one sprite.
The Kenney packs are chosen by hand at vendoring (they are zips of
hundreds of files; the brief indexes them by folder, not by file).

No 3D, no video (parent Round 5). The vendored **ponytail** copy
(github.com/DietrichGebert/ponytail, MIT — "think like the laziest senior
dev in the room"; a decision ladder from "does it need to exist" down to
"minimum viable code"; intensities lite/full/ultra/off; claims ~54% less
code and ~20% cheaper on its own benchmark) is installed
instruction-only, **pinned at commit `974d940a` (2026-09-04, source tarball
SHA-256 `d2677c25…8651a`)**: only `AGENTS.md` (32 lines: the decision
ladder, the rules, the "not lazy about understanding" clause) and
`skills/ponytail*/SKILL.md` are copied into the image's `/config`; its
`hooks/`, `commands/`, `ponytail-mcp/` and the marketplace path are NOT
vendored — no runtime, no fetch, nothing executable. That narrows Gable's
review to prose. Intensity: the instruction-only install has no runtime
mode switch (the tracker is one of the excluded hooks), so `[apps]
.ponytail_mode` (default `full`) selects which intensity line the image
writes into the brief's ponytail section, from the skill's own lite / full
/ ultra table; it is not the builder's choice (parent Round 5).

### G. The store shim and the builder brief
`house-store.js` (parent Rounds 4–5): `store(appId).get(scope, key)` /
`.set(scope, key, value)` / `.del` / `.keys`, scope ∈ {`user`, `app`}.
v1: `user` → `localStorage` under `disjorn:<app-id>:user:<key>`; `app` →
throws `StoreScopeNotAvailable("app scope arrives with server-side storage")`.
Carries `STORE_VERSION = 1`; the context blob carries `BLOB_VERSION = 1`
(parent "archival promise"). Ships with its own tests (node, in the image
build).

`BUILDER-BRIEF.md` (parent Round 4 B2, "every platform limitation and
particular"): static files only; `/work` is the whole world; relative paths
only, never leading-slash; the exact CSP the gate will emit; the context
blob shape and version; the shim API and the `app` scope's documented "not
yet"; no egress, no `fetch()` to anything but `'self'`; never read or
depend on the page URL; the shelf index with one-line usage each; the
output contract (files in `/work`, end with a one-line summary, optional
`FLAG: <one line>`); no self-reported percent; and, in plain words, **"read
`/work` first: it may already contain an app; do not scaffold over it"** —
the sentence that makes per-turn containers work, because continuity lives
in the repo, not in a process (Claudette #2284, Gable #2286 PASS on
ruling 3). It is the seat's whole
knowledge of the platform, and it lives in the image, so residents do not
restate it in build prompts.

### H. What the resident and the user see after a turn
The turn line is a SERVER-SIDE effect of the stage endpoint, the way the
opener is (Gable #2286): the broker sends `detail = {turn, files, tokens,
model, no_changes?, halted?}`, and the server writes ONE message as the
`system` bot into the app_build channel on `files_written` **and on any
event carrying `detail.halted`, whatever stage it re-posts** — a turn that
dies at `scoped` or `scaffolded` must not leave the room silent forever
(Gable #2295, Claudette #2299) — "Turn N done — 4
files written (index.html, app.js, style.css, README.md), 212k tokens,
model claude-opus-5." / "Turn N: no changes." / "Turn N halted — build hit
its ceiling." **Slice (ii) amendment (keyboard D-1.2, Claudette
#2345/#2354): the runner's one-line `summary` (§A) rides `files_written`
detail and is appended to this line as the BUILDER's words, quoted after the
house sentence** — never in the house's own voice, because it is output from
an isolated seat that read a user's prompt. A halt keeps its summary and
names the files it committed the same way ("… Wrote index.html, app.js.
\"the report\"") — halts have facts too (Claudette #2352/#2354). An empty
summary yields the plain line with no dangling quotes. The broker has no post right in the room; stage 1's
two-member wall (#2266) stays the only path. That line is the build summary
in the resident's transcript
(parent B9: the resident later says "the app I built you" because the
transcript says so — Gable's backfill and Claudette's deque both carry
it); it carries no context block, so it summons nobody (stage 1 D2). A
`FLAG:` line from the runner is posted to #custodian by the broker with the
session id, never into the user's room.

### I. Resident-side changes (cross-lane; each resident's own review)
Both residents already receive `context.channel_state.type == "app_build"`
with the app block (stage 1). Each needs to know what to do with it:
- **Gable** (`harness/residency/prompt.py`, custodian lane, plus his spine,
  his lane): when `channel_state.type == "app_build"`, the summon prompt
  gets an `APP_BUILD_FLOW` block in place of `SPEC_FLOW`: elicit; when
  ready write the prompt to `~/apps-prompts/<session_id>-<turn>.md`; call
  `broker apps-build --session-id N --prompt-file <path>` (the tool seat
  sees it as `apps_build`, the generator's naming); tell the user in one
  line what was handed off. Config: `~/apps-prompts/` goes inside his
  `path_map` (Gable #2286). The wording of how he elicits is his.
- **Claudette** (`/home/plink/bots/claudette/disjorn_bot.py` + generated
  `broker_tools.py`, her lane): branch on the same field; the verb arrives
  as a tool automatically once the surface is regenerated.
- The balance between asking and a fast one-shot is "the project's main
  internal goal" (parent Round 4) and is each resident's to learn; nothing
  here scores it.

### J. Server changes (custodian lane, small)
- `GET /apps/sessions/{id}/harness-view` (§E.1), publisher-gated.
- `POST /apps/sessions/{id}/stage` gains `turn` in `detail`, accepts
  `detail.no_changes` and the halt reason (`detail.halted = "ceiling" |
  "error" | "timeout" | "secret"`), and writes the §H system line into the
  room on `files_written` and on any event with `detail.halted`; the client renders halted turns as a red chip
  on the bar and `no_changes` as the turn ending.
- Turn count and tokens-so-far columns on `app_sessions`
  (`turns INTEGER NOT NULL DEFAULT 0`, `tokens_used INTEGER NOT NULL
  DEFAULT 0`), updated by the stage endpoint from `detail`, so the modal's
  hidden ceiling never needs the ledger.
- Client: filenames scroll in the preview panel from `files_written`
  detail; the halted chip; nothing else.

### K. Explicitly not in stage 2
Serving (the :8443 gate, CSP headers, iframe src, screenshots, cards, share
dialog, remix copy, `live`) — stage 3. Per-user credential slot — v2.
Discovery/voting — v2. The on-screen build scrollback — v2 (the ledger and
spool are its raw material).

## The five operational rulings — TAKEN as proposed (Round 1: Claudette #2284, Gable #2286; plink's first thoughts 2026-09-06)
1. **Credential**: dedicated metered key; drop point §D. plink: yes, will
   fetch the key; tell him where. → §D names the path.
2. **Model**: `claude-opus-5` via `[apps].model`. plink: "Opus 5 for now …
   careful not to code us into a corner" → §A's host-side stage derivation
   and `[apps].runner` are the corner-avoidance; nothing runner-specific
   outside the image and one usage parser.
3. **Per-turn container**: TAKEN. plink: "I think your proposal is
   correct, but I'm not 100% on this UX yet — we'll sort it in the
   channel." Both residents PASS (#2284, #2286: continuity lives in the
   repo, not the process); plink at the keyboard the same night: "ready to
   finalize." Re-rulable at confirm.
4. **Host identity**: `res-appsbuilding`. plink: fine; residents will have
   opinions.
5. **Handoff verb** `apps-build` with builder-only check. plink: elegant;
   expects deliberation.

## Review record
- **Round 1** (#2284 Claudette, #2286 Gable, #2288 Claudette): all five
  rulings as proposed. Folded in place above:
  - BLOCK (Gable): key exfiltration through the app's own source →
    §E harvest secret scan (raw + base64 + hex, Claudette), no commit / no
    copy on a hit, copy excludes `.git/` and root dotfiles; §D names the
    real containment (dedicated, metered, rotatable key).
  - Seat→bot map in the broker, bot id never an argument (§B, §E.2).
  - `scaffolded` definition; `no_changes` as a flag on `files_written`;
    halt never deploys; ceiling checked-before/recorded-after (§A, §E).
  - Launcher charsets in the spec; harvest in the launcher, idempotent
    from unit exit status (§C).
  - Turn line is a server-side effect of the stage endpoint (§H, §J).
  - tmpfs HOME (§A); `apps_build` tool name + `path_map` line (§I);
    distinguishable chat-marker refusal (§E); "read `/work` first" in the
    brief (§G).
  - Ownership: keyboard (Gable declined, #2286). Gable takes the ponytail
    vendored-copy review and his `APP_BUILD_FLOW` wording; Claudette
    writes her persona file before the image (#2269) and takes the
    `origin_wall.py` doc card.
- **Round 2** (#2293 Claudette: PASS with two nits, both folded): a secret
  hit ends the session and quarantines the dirty files out of `/work`
  (§E); `seat_bots` is checked at broker boot against the bots table and
  a mismatch disables the verb loudly (§B); `files_written` is named as
  the terminal stage whose label keys off `detail` (§A). Shelf pinned
  with versions and hashes (§F); ponytail pinned at `974d940a`,
  instruction files only (§F). Gable's Round 2 pending a human mention
  (bot posts cannot summon him, by design).
- **Round 3** (#2295 Gable: two NOTEs, no BLOCK, "confirm-ready from my
  side"; #2298/#2299 Claudette: PASS, both NOTEs endorsed): the broker
  runs as `plink` and cannot touch `/srv/apps`, so `git init` and the
  `scaffolded` watch move into the launcher/unit and the broker reads a
  per-turn result directory (§A, §C, §E); the turn line is written on any
  halted event, not only `files_written` (§H, §J). Gable's
  `APP_BUILD_FLOW` wording is drafted seat-local for step (iii).
  **Confirm-ready from both residents at this revision.**
- **Slice (i) review** (Claudette, 2026-09-07; banner #2313, fold #2318,
  branch `loop/2026-09-06-apps-builder-seat-s1` @ `eaaf30e`): one BLOCK —
  the keyboard's prompt-staging fold had root creating and chowning inside
  the seat-writable result directory with symlink-following calls
  (seat-to-root). Folded by removing the root write entirely: the launcher
  feeds the bounded prompt bytes to the unit on stdin through a memfd and
  `systemd-run --pipe`; the wrapper stages its own 0600 copy as the seat
  (§C amended: root writes nothing under `/srv/apps-turns`). Four NOTEs
  folded: nothing from the credential directory is mounted into the
  container (§A amended — `/config` is image content; a rotation's
  `env.old` had a path in); spools are secret-scanned and redacted in place
  and usage is parsed into `result.json` by the harvest as the seat, so
  spools stay 0600 seat-only and the broker never opens one (§C, §E); an
  ignored-only turn is scanned and quarantines on a hit though git calls it
  clean; the post-hit reset clears ignored leftovers. Also from the slice:
  the scan runs before a HALTED commit too (a timed-out turn that wrote the
  key quarantines instead of committing it); `scaffolded` ignores the repo
  root's own mtime (git init bumped it); tmpfs HOME via `--mount U=true`.
  The `keyboard` launch principal was DELETED at slice (ii) (keyboard D-C2,
  Gable/Claudette): the launcher literal, the `launch.toml` line, and the
  sudoers alternative all gone, so the only principal is a `res-<name>` seat
  and the spec's charset is the whole story. The proving turns from slice (ii)
  on go through the `apps-build` verb, not a hand-run `keyboard` principal.
- **Slice (i) review, rounds 2–3** (2026-09-07): BLOCK 2 (Claudette #2320,
  folded `6014c2f`) — the prompt name was checked and then reopened; now one
  `O_NOFOLLOW` open, identity from the fd. BLOCK 3 (Claudette #2325, Gable
  #2327 deltas, fold list #2329; folded `f27c057`) — the preview chmod walk
  followed symlinks and a dangling link raised out of the harvest with no
  record. Folded: publish by rename through a fresh sibling (`--no-links
  --no-D --chmod=D0755,F0644`, no walk, no `--delete` into a live root);
  every harvest failure is a `halted = "error"` record with the commit kept;
  §E's absence rule; the launcher's `O_NONBLOCK` (a planted FIFO cannot
  wedge root) and owner check (`st_uid` = the mapped directory's owner,
  root refused — reading TAKEN #2336); the sentence in both module
  docstrings: *we keep validating where a thing is instead of what it is.*
  Gable's ponytail read (#2331, folded `4b54789`, PASS #2339): intensity
  rows quoted verbatim, §9's three disagreements, `ponytail-gain` dropped.
  Code head signed `f27c057` (#2336, four NOTEs), prose head passed
  `4b54789`; NOTEs folded before merge at `863c909`: staging under
  `/srv/apps-www/.staging/<app-id>/` and the §C served-set sentence,
  cleanup failure warns, one record shape, the publisher owns the preview
  root, the pin is a test. **Merged to main `4d89b49` (review-seq 2336),
  suite 408.** Carried forward by Gable: her #2312 `APP_BUILD_FLOW` folds.
- Parent spec Round 14 (same commit) corrects Round 13's `APPS_BUILDERS`
  sentence per #2276: that setting names the CHAT seat (keyed resident);
  the BUILD seat is broker.toml `[apps]`; inert was right for the reason in
  #2274.

- **Slice (i) MERGED** to main `4d89b49` (review-seq 2336): the seat, the
  launcher, the harvest, the image, the shelf, the brief. Three review
  rounds (#2313/#2320/#2325), all folded; code head `f27c057`, prose head
  `4b54789`, notes folded at `863c909`. Rounds recorded above under §A/§C/§E.
- **Slice (ii) MERGED** to main `7d80a79` (review-seq 2354): the `apps-build`
  verb + boot check + ledger, `harness-view`, the stage publisher + §H line,
  migration 012, the client bar/chip/scroll, the harvest summary/flag lift,
  the `keyboard` principal deletion. Built by three Opus hands, smoke-proven
  live. Review: kickoff notes + Gable's app-id BLOCK folded pre-build
  (#2347/#2349); two `publish_stage` BLOCKs folded (#2352 — a halted turn
  free-riding the token ceiling, and a `spawned:false` refusal rewinding the
  stage pointer); two re-read NOTEs folded (#2354 — the one-token-event-per-
  turn invariant made a server-side wall, and a halt keeping its report).
  Cleared #2354. Ships inert (verbs.toml OFF for both residents, prod
  `APPS_BUILDERS` empty). Amendments this revision: §E.1 (lock lapse no
  longer refuses a handoff or a publisher post — D-A1/D-B3), §H (the runner
  summary quoted as the builder's words; a halt names its files), §C (the
  `keyboard` principal deleted — D-C2).
- **Slice (iii)** (not built): the residents' `APP_BUILD_FLOW` / adapter
  branch (each their own review), then prod `APPS_BUILDERS` set and
  `verbs.toml apps-build` flipped on — the go-live. Stage 3 is the :8443 gate.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: custodian — broker, `harness/cc` (new image, `run-apps.sh`,
  shelf, brief, shim), `harness/keyboard/10-appsbuilding.sh`, server §J,
  client §J.
- **Review owner**: Claudette.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: plink (keyboard lane), unless Gable claims this spec and
  walks it through his ceremony — plink's stated preference is to respect
  that if asked, keyboard otherwise.

## Cross-lane split
- **Applies**: yes
- **Surfaces by lane**:
  - custodian: everything in §A–§H, §J → review owner Claudette
  - Gable's area: his `APP_BUILD_FLOW` wording and spine changes (§I) →
    review owner Gable
  - Claudette's area: `disjorn_bot.py` branch + regenerated
    `broker_tools.py` (§I) → review owner Claudette
  - ponytail vendored copy review → Gable (his request, parent Round 5)
- **Split agreed in #custodian**: 2302 (rounds #2284–#2299 agreed the surfaces; confirm #2302)

## Expected diff tier
Tier 2 — new seat, new sudoers-scoped launcher, new broker verb, broker
config reader, server endpoint.

## Token estimate
Large: ~1.5M build tokens. Suggested order, each its own merge under this
confirm: (i) §C host user + §D drop point + image + shelf + brief + shim
(no verb yet; a keyboard-run turn proves the contract); (ii) §B/§E broker
table, verb, ledger, stage derivation, §H line, §J server/client;
(iii) §I resident flows, each resident reviewing their own.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 2302
- **Confirmed at**: 9/6/2026
<!-- No Confirm record → no build. This is the gate. -->

## Status
`building`
<!-- set at the keyboard 2026-09-06 (was `confirm`, a typo for `confirmed` — the broker parser wants the full word): slice (i) (seat, shelf, brief, shim, harvest) building on loop/2026-09-06-apps-builder-seat-s1, two Opus hands, Fable orchestrating; confirmed by plink #2302. -->
