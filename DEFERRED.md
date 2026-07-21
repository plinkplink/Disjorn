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

## BUILD-LOOP red-team — findings (2026-07-21)

Two adversarial verifiers (independent, cross-validating) attacked the five
build-loop WPs. Server 162 tests green, harness 61 residency + 69 broker green.

**Fixed in this pass (not deferred), with regression tests:**
- **Privacy-wall bypass via `/backlog` (HIGH, same class as the WP15 DM-bot
  leak).** `create_message` flags-and-hides a `/backlog` message from bots,
  then the old code copied the identical text into the bot-readable `backlog`
  table + `GET /backlog` + the server-rendered listing — leaking secret /
  off-the-record content the server itself had just hidden. Fix: thread the
  message's effective privacy flags into `slash.dispatch`; the filing handler
  refuses at intake (`privacy.hidden_from_bots`) so no flagged row is ever
  written. Refusal text never echoes the content. Read side needs no filter —
  the table is public by construction now. Tests: test_backlog_refuses_*.
- **Model-suffix fail-open (L5).** When a session reported no model id
  (`actual=None`), the reply suffix stamped the *pin* as if confirmed —
  inverting "shows what's actually running." Fix: `format_reply_suffix(...,
  verified=False)` marks it `(pinned; actual unverified)` and the adapter logs
  an unverified warning. Tests: test_suffix_shows_pin_when_actual_unknown
  (updated), test_suffix_verified_flag_shapes.

**Deferred — ACTIVATION BLOCKERS (must be closed before the verb/flag is
flipped ON; the code ships OFF so nothing bites until then):**

- **BL-D1 — Confirm gate is integrity-by-config; enforce `specs_dir` is
  resident-unwritable (HIGH-at-activation).** Both verifiers' headline. The
  confirm record (`Confirmed by` + `#custodian seq`) is a presence-check on
  caller-controlled text — by design advisory (BUILD-LOOP: verbs.toml toggle
  authorizes the class, the record selects the instance). The REAL instance
  gate is that `specs_dir = /srv/disjorn-ro/SPECS`, the plink-gated RO mirror
  residents can't write. That invariant lives only in a comment. Repointing
  `specs_dir` at a resident worktree silently converts self-attestation into a
  self-authorization bypass (any resident forges a confirm record, builds
  within the 2/day budget). Fix (witnessed design — protected authorization
  surface, treat like D3): at start-build config load, hard-assert
  `realpath(specs_dir)` is not inside any resident-writable volume (fail loud);
  and/or cross-check the `#custodian seq` against real chat history before
  launch. NB: **required before start-build ships.**
- **BL-D2 — Reaper unbounded-stdout OOM on the privileged broker (MEDIUM,
  insecure-as-written when ON).** `_reap_build` calls
  `proc.communicate(spec_bytes, timeout=...)` with no size bound on a
  resident-influenced build session (up to `timeout_sec`, 3600s default); a
  build flooding stdout balloons the broker's RSS (measured 180MB stdout →
  540MB broker RSS) and can OOM the verb gateway for all residents. Fix
  (touches the just-built + tested detachment I/O contract — witnessed):
  redirect the build's stdout/stderr to a temp file at spawn (bounded on disk),
  read a bounded tail for the done-report; or bounded-read with kill-on-
  overflow. **Required before start-build ships.**

**Deferred — lower severity (file, fix opportunistically):**
- **BL-D3 — Build-budget reseed overcounts a never-started build across a
  broker restart (LOW).** A spawn `OSError` refunds the in-memory slot
  (`_release_build`) but still audits `allowed=True` (exec-failure is not a
  denial), and `_count_builds_today` recounts `allowed=True` lines on restart
  reseed — so a build that never ran consumes a slot post-restart. In-memory
  and on-disk disagree. Budgets are soft (kill switches are the hard lever),
  cap default 2. Fix: distinguish never-started from ran-and-failed in the
  audit (e.g. a `started` marker the reseed counts), or audit spawn-failure as
  non-consuming.
- **BL-D4 — Slug collision → branch/container-name clash (LOW footgun).** Two
  specs with the same base slug but different dates both derive `loop/<slug>`
  and `disjorn-build-<slug>`; concurrent → podman `--name` clash, sequential →
  second clobbers the first's branch. No privilege issue. Fix: uniqueness check
  or date-in-slug.
- **BL-D5 — Backlog has no visibility scoping; DM-filed items exfil to public
  chat (LOW/MED).** `/backlog <text>` files from any channel incl. DMs into one
  global table; `/backlog` (no args) in a public channel dumps every item +
  author verbatim, and `GET /backlog` returns all to any authenticated actor.
  A non-flagged sensitive request filed in a DM leaks to public via one listing
  (the privacy-flag fix above only blocks secret/off-the-record content, not
  merely-sensitive text). Backlog is "public feature requests by design"
  (Architecture §13), so this is a footgun not a wall breach. Fix: warn/refuse
  on filing from a DM, or scope reads. Pairs with:
- **BL-D6 — No rate limit or content-length cap (LOW/MED, pre-existing gap
  backlog widens).** `create_message`/`dispatch` have no throttle;
  `MessageCreate.content` has no `max_length` (a 2MB `/backlog` stored
  verbatim). `GET /backlog` is unpaginated. Fix: cap message/backlog text
  length, paginate GET /backlog, consider a per-actor command rate limit.

**Governance decision owed to plink (not a code fix — a ratified-spec
reconciliation):**
- **BL-G1 — Model integrity: "refuse to act" vs alert-only.** BUILD-LOOP item 2
  (ratified) says mismatch → "refuse to act + alert." The shipped adapter is
  alert-only: the actual model is only knowable from the FINISHED session's
  output envelope, so the check is post-hoc and the reply goes out before the
  drift alert ("fail-loud, never fail-over"). This silently softens a ratified
  line. plink to either (a) re-ratify alert-only as the contract, or (b)
  greenlight a real pre-act gate via `--output-format stream-json` whose
  `system/init` event reports the model BEFORE the turn completes, enabling an
  early abort (a fast-follow WP). Recorded as an open decision in BUILD-LOOP.md.
