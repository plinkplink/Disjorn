# Disjorn — Light Discord Clone — Spec v1.1 (build-ready)

v1.1 incorporates the planning review: server-enforced privacy, bot/DM visibility rules,
channel membership + read state, edit/delete semantics, first-class Web Push notifications,
signed media URLs, and resolved stack choices. HTML rendering and email notifications moved to v2.

---

## 1. Scope & Constraints
- **Users:** 4–5 humans + N chatbots (Python-native; Claudette et al.).
- **Deployment:** Self-hosted on local Debian machine, behind Tailscale (tunnel exposure optional).
- **Client:** Responsive PWA (desktop browsers + Android browsers). No native apps.
- **Auth:** Admin-created accounts only. No open registration, no phone/email verification, no OAuth.
- **Encryption:** None (E2E overkill for v1). HTTPS/tailnet transport security only.
- **Scale:** Single server/guild for v1. Multi-server deferred.

---

## 2. Architecture Overview
```
Client (PWA, React/Vite/TS) <--> REST + WebSocket <--> Server (FastAPI, Python 3.11+)
                                                    |
                                          SQLite (WAL) + FTS5
                                                    |
                                  Local Disk (attachments, picker images, chibi packs)
                                                    |
                              Pluggable services: faster-whisper (STT),
                              Ollama (summarize), Web Push (pywebpush)
```
- **Real-time:** Single WebSocket hub; separate auth paths for humans (session cookie) and bots (API key).
- **Internal event bus:** modules publish domain events (`app/events.py`); the WS hub subscribes and fans out. This keeps modules decoupled and gives a single choke point for privacy filtering.
- **File storage:** Local filesystem; attachments served via **signed URLs** (HMAC + expiry). Server converts HEIC/RAW/oversized phone JPEG to web formats.
- **Backup:** Existing server backup covers disk. **SQLite must be backed up via `sqlite3 .backup` or Litestream — not a raw copy of a live DB.**

---

## 3. Auth & Identity (Flat)
- Username + password (argon2id hashing). Flat access — no roles beyond a single `is_admin` bit used only for account management.
- **Registration:** Admin creates accounts via CLI/admin endpoint. Open registration removed.
- **Sessions:** Opaque token in an HttpOnly cookie, server-side session table.
- **Bots:** Per-bot API key (hashed at rest), used for WS auth and REST.
- **Profiles:** Display name, optional avatar upload, status (online/idle/dnd), presence derived from WS connections.

---

## 4. Entities & Data Model

### 4.1 Core Entities
| Entity | Key Fields |
|---|---|
| `User` | `id`, `username`, `password_hash`, `display_name`, `avatar_path`, `status`, `is_admin` |
| `Session` | `token`, `user_id`, `created_at`, `expires_at` |
| `Channel` | `id`, `type` (`main_feed`, `dm_1to1`), `name`, `created_at` |
| `ChannelMember` | `channel_id`, `member_type` (`user`\|`bot`), `member_id`, `last_read_seq` |
| `Message` | `id`, `channel_id`, `seq`, `author_type` (`user`\|`bot`), `author_id`, `content`, `created_at`, `edited_at` (nullable), `deleted_at` (nullable, soft delete), `reply_to_id` (nullable), `privacy_flags` (JSON), `emote_refs` (JSON, bot messages) |
| `Attachment` | `id`, `message_id`, `file_path`, `original_filename`, `mime_type`, `size_bytes`, `width`, `height` |
| `Bot` | `id`, `name`, `api_key_hash`, `avatar_path`, `chibi_pack` (nullable path) |
| `PushSubscription` | `id`, `user_id`, `endpoint`, `keys_json`, `created_at` |
| `messages_fts` | FTS5 virtual table over `Message.content`, trigger-maintained |

- **`seq`:** monotonic integer **per channel**, allocated only for persisted messages. Ephemeral events (typing, presence) carry **no seq** — they cannot be backfilled.
- **Membership:** `main_feed` implicitly includes all users; DM channels have exactly two user members. Bots are members of `main_feed` by default; **bots are members of a DM only if explicitly added**.
- **Read state:** `last_read_seq` per member per channel drives unread badges and notification suppression.

### 4.2 Message Metadata
- `reply_to_id`: reply feature; original message shows "replied to" backlink.
- `privacy_flags` (JSON object): `secret`, `off_the_record` (`ephemeral` cut in v1.1).
  - **Enforcement is server-side.** Flagged messages are excluded from the bot event stream, bot backfill, and bot context injection at the server. Bots never receive what they must not remember.
  - NL triggers ("don't tell anyone, but…") are detected **server-side before fan-out** (simple keyword/regex match in v1), so the flag exists before any bot sees the message.
  - Bots may additionally set flags on messages they create.

---

## 5. Messaging Features

### 5.1 Text Chat
- Persistent archive, soft-deleted messages retained in DB but hidden from all reads.
- Real-time delivery via WebSocket; REST for history/backfill.
- Full-text search (FTS5) for users; excludes deleted messages.

### 5.2 Replies (Not Threads)
- Composer quotes truncated original; sent message carries `reply_to_id`; original renders a backlink indicator.

### 5.3 Image/GIF Picker (formerly "reactions")
- Not message-attached reactions — a **picker that posts an image into the chat**.
- Tabbed picker: `gif` | `image`; server-hosted static images under `/assets/picker/`.
- No emoji/stickers. No `Reaction` entity.

### 5.4 Chibi Emotes (bot messages) — stub for later build-out
- Bot tags an **emotion** in its message payload (e.g. `emotion: "Smug"`); backend resolves it to a chibi image from the bot's configured chibi pack and attaches it as `emote_refs`.
- Pack layout follows Claudette's existing `/home/plink/bots/claudette/chibis/` convention: category directories of PNGs + `Emotions.txt` taxonomy.
- Rendered at **beginning or end** of the bot message body (never inline with text flow).
- v1 ships: `services/chibi.py` resolver + rendering; bots may also pass explicit `emote_refs` by name.

### 5.5 Formatting
- Discord-flavored markdown only: `**bold**`, `*italic*`, `__underline__`, `~~strike~~`, `` `inline code` ``, ``` fenced code blocks ``` (custom renderer config — note `__x__` is a Discord-ism).
- Code blocks render distinctly (monospace block, copy button).
- **HTML rendering: moved to v2** (with `nh3` as the chosen sanitizer when it lands).

### 5.6 Link Unfurling & Summarization
- URLs unfurled server-side (OpenGraph title/description/thumbnail), cached.
- **Summarize** icon next to unfurl → modal with summary, Close, and "Share to channel" (posts summary + link).
- Engine: local model via **Ollama** behind a pluggable `services/summarize.py` interface (models will be swapped over time — keep plumbing loose).

---

## 6. Media & Attachments
- Large max size (trusted users); images, documents, generic files.
- **Phone images:** accepts HEIC/HEIF, RAW/DNG, oversized JPEG; converts to web format (WebP/JPEG) for display; original preserved on disk.
- **Access control:** all attachment/avatar URLs are **signed (HMAC + expiry)** — DM attachments are not readable by URL-guessing or other tailnet users.
- Image preview modal in client; thumbnails generated on upload.

---

## 7. Voice-to-Text
- Mic icon in message input → hold/click to record (MediaRecorder) → POST audio → server transcribes → text inserted into input box (user edits, then sends).
- Engine: **faster-whisper** on the RTX 5090, behind a pluggable `services/stt.py` interface for drop-in model replacement.
- Future: auto-post, diarization.

---

## 8. Bot Integration Contract

### 8.1 Authentication
- API key per bot (header for REST, first-frame auth for WS).

### 8.2 Event Stream (WebSocket)
Server pushes **full materialized events** (not diffs) for channels the bot is a member of,
**after privacy filtering** (no `secret`/`off_the_record` content ever reaches a bot).

| Event Type | Payload |
|---|---|
| `message_create` | Full message object incl. `seq`, `reply_to`, `attachments[]`, `emote_refs` |
| `message_edit` | Full updated message object + `edited_at` |
| `message_delete` | `id`, `channel_id`, `seq` |
| `typing_start` | `author_id`, `channel_id` (no seq) |
| `presence` | `user_id`, `status` (no seq) |

**Protocol rules:**
- Persisted events carry `seq` (per channel) + wall-clock `timestamp`; bots track `last_seen_seq` per channel.
- **Backfill = current state, not event replay:** on reconnect, `GET /channels/{id}/messages?from_seq=X` returns messages in their *current* state (edits applied, deletions omitted/marked). Bots reconstruct "what's true now."
- No polling.

### 8.3 Structured Context Injection
Per bot invocation (@mention or keyword), the server appends a JSON context block:
```json
{
  "awake_users": [{"id": "u1", "name": "...", "status": "online"}],
  "channel_state": {"name": "#main"},
  "privacy_flags_on_current_message": {}
}
```
Built server-side from already-filtered data. Structure open for future metadata.

### 8.4 Bot Message Posting
- REST POST or WS send; supports formatting, `emotion`/`emote_refs`, `privacy_flags`, `reply_to_id`.

### 8.5 Bot Memory
- **Bot-side, not platform-side.** Bots reuse Claudette's existing ChromaDB + Voyage stack (`bots/claudette/memory/`). Disjorn's job is to make that easy: clean SDK, seq-based backfill, filtered stream. No memory reimplementation in the platform.

### 8.6 Deferred
- Scratchpad, bot-to-bot channel: post-MVP. Additional agent-facing API endpoints added as needs surface.

---

## 9. Notifications (first-class, v1)
- **Web Push** (service worker + self-hosted VAPID keys, `pywebpush`). No third-party service, works on Android PWA installs.
- Push sent for: DM messages, @mentions, (configurable) all main-feed messages — suppressed when the recipient has the channel focused/read.
- Unread badges per channel from `last_read_seq`.
- Email digest/piggyback: **v2** (pending exim4 smarthost setup).

---

## 10. UI / UX (PWA)
- Discord-dark-inspired, mobile-first responsive; improved where our use case allows (single guild → no server rail).
- Layout: minimal sidebar (channel list: #main + DMs + user presence list) + chat feed; input bar with attachment, picker, mic buttons.
- Modals: summarize, image preview.
- Installable PWA (manifest + service worker); push permission prompt in settings, not on load.

---

## 11. Security & Privacy Model
| Layer | Approach |
|---|---|
| Network | Tailscale/private tunnel; HTTPS. |
| Auth | Admin-created accounts; argon2id; HttpOnly session cookies; hashed bot API keys. |
| Privacy flags | **Server-enforced** exclusion from bot stream/backfill/context; server-side NL trigger detection pre-fan-out. |
| DMs | Bots excluded unless explicit members; attachments behind signed URLs. |
| Messages | Persistent, searchable; soft delete. |
| Data export | Manual (DB + uploads dir); future feature. |

---

## 12. Stack (resolved)
| Concern | Choice |
|---|---|
| Backend | Python 3.11+, FastAPI + uvicorn |
| DB | SQLite (WAL) + FTS5, numbered SQL migrations, `sqlite3 .backup`/Litestream for backup |
| Frontend | React + Vite + TypeScript PWA (vite-plugin-pwa), zustand state |
| STT | faster-whisper (pluggable interface) |
| Summarization | Ollama HTTP (pluggable interface) |
| Push | pywebpush + VAPID |
| Media | Pillow + pillow-heif (+ rawpy for RAW/DNG) |
| Bot SDK | `disjorn_sdk` Python package (WS events, backfill, posting) |
| Deploy | systemd units on Debian; uvicorn behind Tailscale |

---

## 13. v2 Backlog
- HTML rendering in chat (sanitize with `nh3`)
- Email notifications (exim4 Gmail smarthost)
- Group DMs, threads, multi-server
- Scratchpad channel, bot-to-bot channel, expanded agent API
- Bookmark board, voice/video calls (likely never — other platforms)
- Data export UI
- Message provenance field (Discord bridge / phone / device)
