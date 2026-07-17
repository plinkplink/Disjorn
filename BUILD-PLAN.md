# Disjorn Build Plan — subagent work packages

Each work package (WP) is sized for one subagent one-shot. Agents MUST read
`Architecture.md` + this file's **Conventions** section + the **Interfaces** of any WP they
depend on before writing code. Each WP owns its listed files exclusively — never edit
another WP's files except where an explicit integration point says so.

## Repo layout
```
Disjorn/                      (this repo, /home/plink/Disjorn/Disjorn)
  Architecture.md  BUILD-PLAN.md
  server/
    app/
      main.py                 # FastAPI app; includes all routers; lifespan runs migrations
      config.py               # pydantic-settings; .env
      db.py                   # aiosqlite helper + migrations runner
      events.py               # internal async pub/sub event bus
      models.py               # pydantic schemas shared across modules
      migrations/*.sql        # numbered, additive
      routers/                # one file per WP: auth.py channels.py messages.py media.py
                              #   search.py notifications.py bots_admin.py stt.py summarize.py
      ws.py                   # WebSocket hub (users + bots)
      privacy.py              # flag detection + bot-visibility filtering (single choke point)
      services/               # stt.py summarize.py unfurl.py chibi.py media_convert.py push.py
    cli.py                    # admin CLI: create-user, create-bot, gen-vapid
    tests/
    requirements.txt
  client/                     # Vite + React + TS PWA
  sdk/disjorn_sdk/            # Python bot SDK
  deploy/                     # systemd units, .env.example, README-DEPLOY.md
  data/                       # runtime (gitignored): disjorn.db, uploads/, assets/
```

## Conventions (all agents)
- Python 3.11+, FastAPI, aiosqlite. No ORM — hand-written SQL through `db.py` helpers.
- Integer autoincrement PKs. Timestamps: UTC ISO-8601 strings.
- Every router file exports `router = APIRouter()`; `main.py` includes them all (scaffold pre-wires imports; later WPs fill the files in).
- Auth deps (from WP2): `get_current_user`, `get_current_bot`, `get_actor` in `app/routers/auth.py`.
- Domain events on the bus (`app/events.py`): `publish(event: dict)` / `subscribe(fn)`.
  Event dicts: `{"type": "message_create"|"message_edit"|"message_delete"|"typing_start"|"presence", "channel_id": int|None, ...payload}`. Publishers send **full materialized payloads**.
- Privacy filtering happens ONLY in `app/privacy.py` (WP5 owns it); everything bot-facing goes through it.
- Tests: pytest + httpx `AsyncClient` + tmp SQLite per test. Each server WP adds tests for its module and leaves the full suite green (`cd server && python -m pytest`).
- Frontend: TypeScript strict, zustand for state, no CSS framework — hand-rolled CSS with design tokens in `client/src/theme.css`. Discord-dark-inspired, mobile-first.
- Don't commit; the orchestrator commits at checkpoints.

## Phase 1 — Foundation (serial)

### WP1: Server scaffold + DB core
Create `server/` per layout: `config.py` (env: DB_PATH, DATA_DIR, SECRET_KEY, VAPID keys, OLLAMA_URL, STT_MODEL, defaults sane), `db.py` (aiosqlite connection, `fetch_one/fetch_all/execute`, migrations runner applying `migrations/*.sql` in order, WAL mode), `events.py` (async pub/sub), `models.py` (pydantic: User, Channel, Message, Attachment, Bot, events), `migrations/001_init.sql` (full schema from Architecture §4.1 incl. FTS5 table + triggers), `main.py` (lifespan: migrate, seed `main_feed` channel; includes all routers as empty stubs it creates), empty router stub files, `requirements.txt`, `tests/conftest.py` (app + tmp db fixtures), smoke test (app boots, GET /healthz). `.gitignore` for `data/`, venv, node_modules. Create `server/.venv` and verify tests pass.

### WP2: Auth module
`routers/auth.py`: login/logout (argon2id via `argon2-cffi`, opaque token → sessions table, HttpOnly cookie), `GET /me`, profile update (display_name, status), auth deps (`get_current_user`, `get_current_bot` via `X-Api-Key` hashed lookup, `get_actor`). `cli.py`: `create-user`, `create-bot` (prints raw API key once), `gen-vapid`. Session expiry sliding 30d. Tests.

## Phase 2 — Core server (WP3 → WP4 → WP5 serial; then WP6, WP7, WP8 in parallel)

### WP3: Channels, membership, read state
`routers/channels.py`: list my channels (+ unread counts from `last_read_seq` vs max seq), create/get DM (`POST /dms {user_id}` idempotent), mark-read (`PUT /channels/{id}/read {seq}` → publishes nothing; just persists), member listing. main_feed membership implicit for users; DMs exactly 2 users; bots only if explicitly added (admin/API). Tests incl. bot-not-in-DM.

### WP4: Messages module
`routers/messages.py`: create (seq allocation per channel in a transaction; validates membership; reply_to must be same channel; runs `privacy.detect_flags(content)` before insert — stub import OK until WP5 lands, guard with try/except ImportError), edit (author only, sets `edited_at`), soft delete, history `GET /channels/{id}/messages?before_seq=&limit=` and backfill `?from_seq=` (current-state semantics: edits applied, deleted omitted), search `GET /search?q=` (FTS5, membership-scoped, excludes deleted). Publishes `message_create/edit/delete` full-payload events on the bus. Attachments joined into payloads (table exists from WP1; rows created by WP6). Tests incl. seq monotonicity + backfill-after-edit.

### WP5: Privacy + WS hub
`app/privacy.py`: `detect_flags(content) -> dict` (keyword/regex: "don't tell anyone", "off the record", "between us", etc. → `secret`/`off_the_record`), `visible_to_bot(message, bot, channel_members) -> bool` (flag check + DM membership check), `filter_event_for_bot(event, bot) -> event|None`.
`app/ws.py`: `/ws` endpoint. Humans auth by cookie, bots by first-frame `{"op":"auth","api_key":...}`. Connection manager tracks user/bot connections + presence (connect=online, disconnect=offline, manual idle/dnd via `{"op":"status"}`). Client ops: `typing`, `status`. Subscribes to the bus: users get events for their channels; bots get **privacy-filtered** events for member channels only. Presence + typing broadcast (no seq). On @mention/keyword of a bot in `message_create`, append context injection block (Architecture §8.3) to that bot's copy. Tests: fanout scoping, secret never reaches bot, DM exclusion, context injection shape.

### WP6: Media module
`services/media_convert.py` (pillow-heif/rawpy/Pillow → WebP display copy + thumbnail, keep original) and `routers/media.py`: `POST /upload` (multipart, creates Attachment rows, returns signed URLs), avatar upload, signed URL scheme `GET /media/{attachment_id}?exp=&sig=` (HMAC-SHA256 with SECRET_KEY; helper `sign_media_url()` importable by messages/WS payload builders — coordinate: WP4 payloads call it if present), picker assets endpoint `GET /picker?tab=gif|image` listing `data/assets/picker/{gifs,images}/`. Tests with tiny generated images (skip HEIC/RAW fixtures if libs unavailable — interface still in place).

### WP7: Notifications (Web Push)
`services/push.py` (pywebpush send, prune dead subscriptions) + `routers/notifications.py`: `GET /vapid-public-key`, subscribe/unsubscribe endpoints storing PushSubscription rows. Bus subscriber: on `message_create`, push to members who are offline OR not focused on that channel (focus = WS-connected with that channel marked read-current), for DMs + @mentions always, main-feed per user preference (add `notify_all_main` pref column migration). Payload: title/author/body-snippet/channel deep-link. Tests mock pywebpush.

### WP8: Pluggable services (STT, summarize, unfurl, chibi)
- `services/stt.py`: `Transcriber` protocol + `FasterWhisperTranscriber` (lazy model load, config model name) + `routers/stt.py` `POST /stt` (audio blob → text). If faster-whisper not installed, degrade to 501 with clear error.
- `services/summarize.py`: `Summarizer` protocol + `OllamaSummarizer` (httpx to OLLAMA_URL) + `routers/summarize.py` `POST /summarize {url}` → fetch page text (trafilatura or readability fallback to raw) → summary.
- `services/unfurl.py`: OG-tag unfurl with in-DB cache table (migration) + endpoint `GET /unfurl?url=`.
- `services/chibi.py`: pack loader for Claudette-style layout (category dirs + Emotions.txt), `resolve(pack, emotion) -> emote_ref`, integration hook: message create accepts `emotion` for bot authors → sets `emote_refs`. Copy 2–3 sample chibis from `/home/plink/bots/claudette/chibis/` into `data/assets/chibi_packs/claudette/` for tests.
All behind protocols for drop-in model swaps. Tests mock external calls.

## Phase 3 — PWA client (WP9 → WP10 → WP11 → WP12 serial)

### WP9: Client scaffold + auth + shell
Vite + React + TS + vite-plugin-pwa in `client/`. `theme.css` design tokens (Discord-dark-inspired palette), login page, app shell (sidebar: #main, DMs, presence list; collapsible on mobile), API client (`src/api.ts`, cookie auth, typed), WS client (`src/ws.ts`: auto-reconnect, on reconnect refetch channels + backfill `from_seq`), zustand stores (session, channels, messages, presence). Manifest + icons (generate simple SVG-based). Proxy config to server for dev. Builds clean (`npm run build`) + `npm run typecheck`.

### WP10: Chat feed + composer
Message list (windowed scroll w/ history pagination via `before_seq`), Discord-flavored markdown renderer (custom marked/markdown-it config; `__underline__`; distinct code blocks + copy button), replies (quote in composer, `reply_to_id`, backlink indicator + scroll-to-original), edited/deleted markers, chibi rendering on bot messages (start/end), composer (send on Enter, Shift+Enter newline, edit/delete own messages), typing indicator emit + display, image/gif picker (tabbed, posts picked image), attachment upload with progress + previews, image preview modal, unfurl cards + Summarize icon → modal with Share-to-channel.

### WP11: DMs, presence, unread, notifications UI
DM open flow from user list, unread badges (per channel, from read state; mark-read on focus/scroll-bottom), presence dots + status picker (online/idle/dnd), settings page (profile, avatar upload, notification prefs), push subscription flow (service worker push handler, deep-link on notification click, permission prompt from settings only).

### WP12: Voice input + polish pass
Mic button: MediaRecorder → hold-to-record (tap-to-toggle on mobile) → POST /stt → insert text into composer at cursor. Search UI (top bar → results panel → jump-to-message). Mobile polish: layout at 360px, safe-area insets, PWA install prompt hint. Fix any typecheck/build warnings across client.

## Phase 4 — Bots + deploy (WP13, WP14 in parallel)

### WP13: Python bot SDK + example bot
`sdk/disjorn_sdk/`: `DisjornClient` (api_key, base_url) — async WS event iterator with auto-reconnect + seq tracking + auto-backfill, `send_message(channel_id, content, emotion=None, reply_to=None, privacy_flags=None)`, `get_messages(from_seq=...)`, typed event dataclasses. `sdk/examples/echo_bot.py` (echoes @mentions, uses emotion tag). README with porting notes for Claudette (memory stays Chroma+Voyage bot-side; map `on_message` → event iterator). Installable (`pip install -e`), integration-tested against a live local server instance.

### WP14: Deployment
`deploy/`: `disjorn.service` (uvicorn) systemd unit, `.env.example` with every config var documented, `README-DEPLOY.md` (install steps on Debian, venv, client build → served by FastAPI StaticFiles from `client/dist`, Tailscale note, VAPID keygen, backup guidance incl. `sqlite3 .backup`, admin CLI usage). Wire static serving of `client/dist` + SPA fallback into `main.py` (small, coordinated edit).

## Phase 5 — Verification (serial)

### WP15: End-to-end pass
Launch server for real; script: create users+bot via CLI, login, post/edit/delete/reply via REST, WS event assertions (human + bot streams), secret-flag exclusion, DM bot exclusion, upload image → signed URL fetch, search, backfill-after-edit, SDK echo bot round-trip. Fix small breakages found (report anything structural instead of hacking around it). Output: PASS/FAIL matrix.

## Orchestration notes
- Checkpoint commits on branch `mvp-build` after each phase.
- Parallel-safe sets: {WP6, WP7, WP8}, {WP13, WP14}. Everything else serial as ordered.
- Blockers get deferred + logged in `DEFERRED.md`, not solved in-line, unless load-bearing for MVP.
