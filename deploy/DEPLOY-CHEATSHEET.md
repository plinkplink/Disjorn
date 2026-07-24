# Deploy cheatsheet — what goes live when, and what needs a kick

*2026-07-24, written after the avatar outage (server code sat undeployed for
a day because nobody restarted uvicorn). Companion to README-DEPLOY.md.*

## The one rule

Ask: **who holds these bytes in RAM?** Every deploy on this box is one of
three patterns:

| Pattern | Examples | To deploy |
|---|---|---|
| **Read per request** | chibi packs, avatars, picker assets, client `dist/`, DB rows | Drop the file / write the row. Live immediately. |
| **Composed per summon** | Gable's spine, channel backfill | Publish the source. Next summon uses it. No restart. |
| **Imported at start** | server python, Claudette's code + spine, residency daemon code, container env/config | Deploy **then restart the process that imported it** — the disk can be current while RAM is a day old. |

The avatar outage and the 2026-07-22 transcript-dump were both the third
pattern missing its restart (or its copy step). When something "should be
fixed but isn't," diff what's running against what's on disk first.

## Platform (Disjorn server + client)

**Server python** (`server/app/...`) — runs FROM the repo, no `--reload`:
```
# edit, test, commit, then:
sudo systemctl restart disjorn
```
Fingerprint if unsure what's live: `POST /auth/login {}` → flat-string 422
detail = post-2026-07-22 code; list-shaped = stale.

**Client PWA** (`client/src/...`):
```
cd client && npm run build        # no server restart — dist/ read per request
```
Browsers pick it up next load (service worker refreshes the shell).

**Assets the server serves** (chibi packs, `Aliases.txt`, avatars, picker):
just files. Chibi pack index + alias cache invalidate by mtime. Avatars have
a 300s browser cache worst-case; `?v={mtime}` busts it for fresh payloads.

**Emote lexicon** (`server/app/services/emotion_match.py`) is server python
→ restart disjorn. Pack `Aliases.txt` is an asset → live, no restart.

## Claudette (containerized adapter — unit `resident-cc` in `res-claudette@`)

Her process imports EVERYTHING at start, fail-closed. Any change below is
inert until the unit restarts.

**Code** (`bot.py`, `disjorn_bot.py`, `core.py`, services): edit the HOST
repo `/home/plink/bots/claudette` (branch `disjorn-port`), commit, then:
```
cd /home/plink/bots/claudette && ./claudette-update.sh
```
That bundles host → ff-only merges into her volume clone
(`/home/res-claudette/resident-home/bots/claudette`) → restarts
`resident-cc`. **Never edit her clone directly** — the ff-only merge is the
tripwire that catches it; if it refuses, stop and look, don't force.

**Spine** (her prompt — Tier 2, human-witnessed, always):
```
# edit canonical /home/plink/bots/claudette/spine (git), merge, then:
sudo bash harness/keyboard/06-spine-mirror.sh claudette
sudo systemctl --user -M res-claudette@ restart resident-cc
```
Path chain: canonical (plink-owned, in 0700 home) → published mirror
`/srv/disjorn-spine/claudette` (world-readable, plink-owned) → mounted
read-only at `/opt/spine` in her container → composed at import.

**Why the mirror exists** (do not "simplify" it away): containers can't
traverse 0700 `/home/plink`, and the spine must be resident-UNWRITABLE —
if her loaded spine lived in her own writable volume she could rewrite her
kernel with no diff, no review, no #custodian. The classifier only sees
submitted diffs; **placement is the wall**. Copy outward; never open
`/home/plink` inward. Same spirit as `/srv/disjorn-ro` (read-only repo
mirror for in-container review, fetch manually) and
`/srv/disjorn-resident-config`.

## Gable (summon-based resident — `res-gable@`)

**Spine**: same publish step, but he composes FRESH EVERY SUMMON — no
restart, next summon has it:
```
sudo bash harness/keyboard/06-spine-mirror.sh gable   # canonical: bots/fable/spine
```

**Daemon/harness code** (`harness/residency/`, wrappers, sdk): runs from
DEPLOYED COPIES under `/usr/local/lib/disjorn/`, NOT the repo:
```
sudo bash harness/keyboard/05-gable.sh                # re-copies residency + sdk
sudo systemctl --user -M res-gable@ restart gable-summon
```
After any residency change, `diff -rq` repo vs deployed before calling it
live — "verified in repo" is not "deployed" (the transcript-dump lesson).

**Config** (`/srv/disjorn-resident-config/res-gable/{summon.toml,env}`):
read at daemon start → restart `gable-summon`.

## The rest

- **BuildGable**: no process — an API key the keyboard uses. Nothing to
  deploy, ever.
- **Discord Claudette** (`claudette.service`, host): separate legacy unit;
  `sudo systemctl restart claudette` after Discord-side code changes.
- **Broker** (`disjorn-broker.service`): host service → edit, then
  `sudo systemctl restart disjorn-broker`.
- **Bot cosmetics** (avatars, `chibi_pack` column): DB rows + files → live.

## Post-deploy verification habits

1. `systemctl is-active <unit>` + `ActiveEnterTimestamp` seconds ago.
2. One behavioral fingerprint of the NEW code (a changed response shape, a
   log line only the new code emits) — process-up is not code-current.
3. For residents: a live hail in-channel beats any amount of unit status.
