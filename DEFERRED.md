# DEFERRED — known limitations & follow-ups (MVP, post-WP15)

Items deliberately left out of the MVP or discovered during the build and the
WP15 end-to-end pass. Deliberate **v2 features** live in Architecture.md §13
and are not repeated here — this file is the "known rough edges of what
shipped" list.

## server

- **`orig_url` absent from message payloads.** `message_payload()` signs only
  the display variant; the client image modal therefore links the display
  (WebP) variant, not the preserved original. The `/upload` response does
  include `orig_url`, so the plumbing exists — add `orig_url`/`thumb_url` to
  the payload builder when the "view original" affordance lands.
- **No admin surface for bot cosmetics.** `cli.py create-bot` takes only a
  name; assigning a chibi pack (`bots.chibi_pack`) or a bot avatar today means
  editing the DB by hand (`routers/bots_admin.py` is still an empty stub).
  Wanted: `create-bot --chibi-pack`, bot avatar upload, or a filled-in
  bots_admin router.
- **Signed media URLs are re-signed per request.** Two identical history
  fetches return the same messages but attachment `url` values can differ in
  `exp`/`sig` (TTL is computed at payload-build time). Harmless, but it makes
  responses non-cacheable byte-for-byte and can confuse naive diffing.
- **Unfurl/summarize fetch arbitrary URLs, including tailnet/localhost
  (SSRF).** Acceptable under the trusted-user tailnet model of v1; revisit if
  the server is ever exposed more widely (block private ranges or allowlist).
- **STT verified with synthetic audio only.** The live E2E pass exercised
  `POST /stt` (faster-whisper `tiny`, CPU fallback, 200 + text) with a pure
  440 Hz tone — no TTS/speech sample was available on the build box. Real
  speech accuracy/latency on the RTX 5090 with the production model size is
  unverified.
- **Push verified against a stub endpoint.** Live pass proved the full send
  path (pywebpush POST fired, 410 pruned the subscription, log line emitted);
  delivery through a real browser push service is exercised only in
  "needs real device" testing below.

## client

- **Reply backlinks only resolve within the loaded window.** The "replied to"
  indicator/scroll-to-original works when the original message is already in
  the client's message window; there is no reverse-index fetch for originals
  outside it.
- **Sidebar last-message snippet shows raw markdown.** The server sends the
  raw content snippet; the sidebar renders it unformatted (`**bold**` shows
  asterisks).
- **Search-jump can render across a non-contiguous seq seam.** The API-level
  window math checks out (WP15 probe: target seq always inside the
  `before_seq = seq + 10` window), but when the jumped-to window doesn't
  overlap the live tail the feed may visually butt two non-adjacent ranges
  together. Needs a browser session to characterize; consider a "gap" divider.
- **No UI for DM bot membership.** `POST/DELETE /channels/{id}/bots` exists
  (now participant-gated) but the client only lists bots — adding/removing a
  bot from a DM requires the API/SDK.
- **Bot avatars are letter-glyphs only.** Client renders a letter tile for
  bots; pairs with the missing server-side bot avatar upload above.

## sdk

- **No `upload()` helper.** Bots can post text/emotions/replies but attaching
  media means hand-rolling the `/upload` + `/attachments/claim` calls.
- **No WS-send posting.** All posting goes through REST (`send()`); the
  protocol's WS posting path is unused by the SDK.
- **Backfill only covers channels with a known cursor.** A channel the bot has
  never seen an event for (and never `seed_seq()`-ed) is skipped on reconnect
  backfill; the first connect of a fresh client performs no backfill at all
  (documented — use `get_messages()` for boot-time catch-up).

## needs real device

- **Android PWA install prompt** depends on Chrome engagement heuristics —
  verify install flow on a real Android device.
- **Android push + mic end-to-end** (real push service, permission prompts,
  MediaRecorder capture → `/stt`).
- **iOS push requires an installed (Add-to-Home-Screen) PWA** — verify on
  real iOS hardware.
- **Safari mp4/AAC STT path untested** — MediaRecorder on Safari produces
  mp4/AAC; the server accepts it in theory, untested on real hardware.
