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

## WP-H13 red-team — deferred findings (2026-07-20)

Fixed in the H13 pass (not deferred): classifier gate fail-open on malformed
gates (F4), broker range RHS flag-injection (F3), broker path_map fail-open-
by-omission (F2, also fixed live), oversize-request audit gap (F1), audit
file perms 0644→0640. All with regression tests; 210 harness tests green.

Deferred below: the classifier's *derived-artifact* weaknesses. Common shape,
and why they're safe to defer: each is a TWO-STEP vector whose step-1 diff
ALWAYS classifies Tier 2 (it touches a protected file), so a human sees it in
#custodian. What's wrong is the machine-checkable promotion/ban artifact the
diff emits — which only becomes load-bearing once `merge-tier1` automation
exists (it doesn't yet; MERGE-CONTRACT is a draft). They also touch the
classifier's core reachability logic, itself a protected/reviewed surface, so
they deserve a witnessed design cycle, not a rushed patch. Claudette flagged
the reachability class at MERGE-CONTRACT ratification; this is that case, made
concrete by the red-team.

- **H13-D1 — Reachability under-detection (was classifier-agent F1).** A
  protected file wired to a not-yet-existing or computed-load target emits no
  promotion proposal, so step-2 can create that target as a fresh unprotected
  file and auto-merge it. Fix direction: at wire time, follow the import
  string/spec and propose promotion even when the target is absent; or refuse
  to let a protected file reference an unpromoted module.
- **H13-D2 — `from pkg import sub` promotes `__init__.py`, not the submodule
  (agent F2).** The promotion resolver checks package stems before submodule
  candidates, so the actually-reachable code file (`pkg/sub.py`) is left
  unprotected. Fix: prefer the submodule resolution for `from pkg import sub`.
- **H13-D3 — Dynamic-import ban is bypassable (agent F3).** `getattr(importlib,
  "import_module")`, `builtins.exec`, string-split `"__imp""ort__"` all evade
  the literal-token check. This is what makes D1's invisible-wire variant
  work. Fix: widen the ban (any attribute access whose value flows to a call,
  any `getattr` on importlib/builtins) — accepting more false positives, since
  the construct is forbidden anyway. NB: **required before merge-tier1 ships.**

Lower-severity, deferred:
- **H13-D4 — Budget check-then-act race (broker-agent F4).** Concurrent
  dispatches can burst past a numeric daily cap (read-count is not lock-
  guarded with the act+audit). Low: budgets are soft/default-OFF, kill
  switches are the hard lever. Fix: hold the audit lock across count+act, or
  count-with-reservation.
- **H13-D5 — pre-tool-use hook tripwire bypasses (broker-agent F5).** The
  `broker` invocation regex misses leading-whitespace/newline/backtick/abs-
  path/`eval` forms, and the socket-path check is a literal substring the
  `BROKER_SOCKET` env var evades. The hook's own honesty note disclaims it as
  "not the wall" (the broker's SO_PEERCRED + per-verb schema is), so no
  privilege is gained — but the stated tripwire guarantees are weaker than
  written. Fix: match `broker` per-segment with `re.M` + normalize, or drop
  the specific guarantees from the docstring to match reality.
- **H13-D6 — git-config-exec on an attacker-controlled repo (broker-agent,
  undetermined).** classify-diff runs git host-side as plink; a resident-
  writable repo with a hostile `.git/config` could in principle exec as plink.
  Contained today by: git dubious-ownership refusal, 0700 resident homes, the
  now-fail-closed path_map (each resident's map only exposes /opt/disjorn +
  their own volume), and classify-diff shipping OFF for gable. Verify before
  widening any classify-diff grant: point it at a resident-writable repo
  carrying a hostile .git/config and confirm git refuses.
