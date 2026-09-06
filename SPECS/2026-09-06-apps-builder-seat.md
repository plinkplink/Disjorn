# Spec: APPS v1 stage 2 — apps-builder seat, handoff verb, stage publisher

<!--
DRAFT AUTHORED AT THE KEYBOARD 2026-09-06 (Fable, plink's seat) as the
stage-2 cut of SPECS/2026-08-30-apps-tab-v1.md (confirmed #2211, stage 1
merged 40c08a6). It is also the "provisioning spec" that parent spec defers
to in Rounds 4–6 (asset shelf, ponytail, builder brief). Parent rulings are
not re-litigated here; where this file names a ruling it cites the round.
Five operational questions are OPEN below with plink's first thoughts; the
#custodian round on this draft settles them. Gable may claim ownership of
this spec and walk it through his ceremony instead (plink, keyboard); if he
does not, it goes through the keyboard.
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
- **`/config`** (ro): the builder brief `BUILDER-BRIEF.md` (§G) and the
  runner's own config (for Claude Code: a `CLAUDE.md` that is the brief plus
  ponytail's instruction file, hooks, settings). Credential by name only,
  masked from the session the way `run-build.sh` does it.
- **network**: the build-seat policy — pasta plus the WP-H2 nftables wall,
  so the model API is reachable and nothing else is. The seat has no broker
  socket, no gatehouse, no house memory, no platform clone, no spine (parent
  Walls/1). Zero broker verbs by construction: there is no socket to call.
- **stdout/stderr**: the runner's own stream, spooled to 0600 files like
  build seats (BL-D2); read for usage accounting and the optional `FLAG:`
  line, never for stage detection.
- **exit code**: 0 = turn ended by the runner; anything else = halted.

**Stage events are derived HOST-SIDE from the filesystem and the process,
never from runner output.** `scoped` on spawn; `scaffolded` when the first
path appears under `/work` (inotify on the host mount); `files_written` on
exit 0 with a non-empty `git status`; `deployed` after the host copies
`/work` to the preview root. This is the portability wall: a different
model or a non-Anthropic runner is a different image and a different
`[apps].runner_command`, and the stage bar, the ledger and the verb do not
change. The runner-specific bits (stream-json usage fields, the CLAUDE.md
convention) are confined to the image and to one usage-parser function
named per runner.

### B. Runner, model, and the `[apps]` table (broker.toml, plink-owned)
```
[apps]
runner = "claude-code"                     # selects image + usage parser
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
defaults; today nothing reads the table (measured, stage-1 mapping). The
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
world-readable so the stage-3 gate, a house process, can serve it). Transient
units `disjorn-apps-<session>-<turn>.service` run as this user via a
`disjorn-apps-launch` sudoers-scoped launcher, the `disjorn-build-launch`
pattern: validates ids, sets `RuntimeMaxSec`, `MemoryMax`, `LimitFSIZE`,
`TasksMax`, then `run-apps.sh`. New keyboard script
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
metered deliberately).

### E. The handoff verb: `apps-build` (plink: "elegant … extended deliberation")
One new broker verb, enabled per resident in `verbs.toml`
(`[res-gable] apps-build = true`, `[res-claudette] apps-build = true`),
declared in `verb_surface.toml` for both seats (CLI + tool), handler in
`brokerd.py`'s static table, generator re-run, `test_verb_surface.py` green.

Args: `{"session_id": int, "prompt_file": str}`. The prompt is a FILE on the
caller's own volume, resolved through that resident's `path_map`, bounded
by `prompt_max_bytes`, and refused if it carries `[[CHAT]]` markers (the
existing pre-tool-use tripwire already denies those). Chat is data; the
prompt is data the broker reads, never argv a resident composes.

Broker checks, in order, each a flat-sentence refusal in the audit log:
1. session exists, is open, lock not lapsed — read through a new
   broker-only server endpoint `GET /apps/sessions/{id}/harness-view`
   (bot actor whose name is in `APPS_STAGE_PUBLISHER_BOT_NAMES`, i.e. the
   `broker` bot; returns app id, owner id, builder bot id, stage, turn count,
   tokens so far);
2. the CALLER's bot id == the session's `builder_bot_id` (the seat that
   elicited is the only seat that may hand off — the user picked it);
3. ceiling: tokens so far + 0 < `build_token_ceiling` (a session at the
   ceiling cannot start a turn);
4. one turn at a time per session (in-memory claim, sidecar JSON like
   builds, adopted on restart).
Then: post `scoped`; `git init` the app repo if absent; spawn the turn;
watch `/work` for `scaffolded`; on exit derive `files_written` (or a halt);
commit in the app repo as `turn N`; copy to preview root; post `deployed`;
parse usage; append the ledger line; post the §H system line. The verb
returns immediately after spawn with `{turn: N, unit: …}`; the resident does
not wait on it (a summon has a clock).

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

| slot | candidate | license | why this one |
|---|---|---|---|
| 2D game lib | KAPLAY | MIT | small, batteries-included, one script tag; Phaser is the fallback if KAPLAY proves too thin |
| chart lib | Chart.js | MIT | the boring default; one UMD file |
| CSS reset | modern-normalize | MIT | 200 lines, no opinions |
| fonts | Inter, JetBrains Mono | OFL | one UI face, one mono face, woff2 |
| icon set | Lucide | ISC | one SVG sprite, `<use href="#name">` |
| store shim | `house-store.js` (house-written) | house | §G; the only house code on the shelf |
| CC0 pack | a Kenney subset: one sprite pack, one UI pack, one sound pack | CC0 | modest, curated by hand at vendoring |

No 3D, no video (parent Round 5). The vendored **ponytail** copy
(github.com/DietrichGebert/ponytail, MIT — "think like the laziest senior
dev in the room"; a decision ladder from "does it need to exist" down to
"minimum viable code"; intensities lite/full/ultra/off; claims ~54% less
code and ~20% cheaper on its own benchmark) is installed
instruction-only: its `AGENTS.md` content and the `ponytail` skills are
copied into the image's `/config` at a pinned commit, no marketplace
fetch at runtime. `PONYTAIL_DEFAULT_MODE` is an `[apps]` knob
(default `full`), not the builder's choice (parent Round 5). Gable reviews
the vendored copy, as he asked.

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
`FLAG: <one line>`); no self-reported percent. It is the seat's whole
knowledge of the platform, and it lives in the image, so residents do not
restate it in build prompts.

### H. What the resident and the user see after a turn
The broker posts ONE message as the `system` bot into the app_build channel:
"Turn N done — 4 files written (index.html, app.js, style.css, README.md),
212k tokens, model claude-opus-5." or "Turn N halted — build hit its
ceiling." That line is the build summary in the resident's transcript
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
  `broker apps-build --session-id N --prompt-file <path>`; tell the user in
  one line what was handed off. The wording of how he elicits is his.
- **Claudette** (`/home/plink/bots/claudette/disjorn_bot.py` + generated
  `broker_tools.py`, her lane): branch on the same field; the verb arrives
  as a tool automatically once the surface is regenerated.
- The balance between asking and a fast one-shot is "the project's main
  internal goal" (parent Round 4) and is each resident's to learn; nothing
  here scores it.

### J. Server changes (custodian lane, small)
- `GET /apps/sessions/{id}/harness-view` (§E.1), publisher-gated.
- `POST /apps/sessions/{id}/stage` gains `turn` in `detail` and accepts the
  halt reason for a halted turn (`detail.halted = "ceiling" | "error" |
  "timeout"`); the client renders halted turns as a red chip on the bar.
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

## OPEN — the five operational rulings (plink's first thoughts, 2026-09-06)
1. **Credential**: dedicated metered key; drop point §D. plink: yes, will
   fetch the key; tell him where. → §D names the path.
2. **Model**: `claude-opus-5` via `[apps].model`. plink: "Opus 5 for now …
   careful not to code us into a corner" → §A's host-side stage derivation
   and `[apps].runner` are the corner-avoidance; nothing runner-specific
   outside the image and one usage parser.
3. **Per-turn container**: proposed yes. plink: "I think your proposal is
   correct, but I'm not 100% on this UX yet — we'll sort it in the channel."
4. **Host identity**: `res-appsbuilding`. plink: fine; residents will have
   opinions.
5. **Handoff verb** `apps-build` with builder-only check. plink: elegant;
   expects deliberation.

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
- **Split agreed in #custodian**: <seq — fill at confirm>

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
- **Confirmed by**: <username>
- **#custodian seq**: <seq>
- **Confirmed at**: <timestamp>
<!-- No Confirm record → no build. This is the gate. -->

## Status
`draft`
