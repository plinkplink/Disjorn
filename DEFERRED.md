# DEFERRED — known limitations & follow-ups (MVP, post-WP15)

Items deliberately left out of the MVP or discovered during the build and the
WP15 end-to-end pass. Deliberate **v2 features** live in Architecture.md §13
and are not repeated here — this file is the "known rough edges of what
shipped" list.

---

## STATUS 2026-07-22 — closure sweep (Opus keyboard session)

A wave of parallel WPs closed most of the standing backlog. **Everything below
shipped CLOSED**: no verb was flipped on, no scheduled job was enabled, no live
resident behaviour was changed. Activation remains plink's, after the red-team.

**CLOSED this session** (details in the sections below and in
RED-TEAM-BACKLOG.md, which is the checklist view):
- **BL-D1, BL-D2, BL-D3, BL-D4** — all four `start-build` activation blockers.
- **BL-D5, BL-D6** — backlog DM-leak + caps/pagination/rate-limit.
- **H13-D1, H13-D2, H13-D3** — classifier reachability + the dynamic-import ban.
  D3 was the stated blocker on `merge-tier1`.
- **H13-D4, H13-D5** — budget check-then-act race; hook tripwire honesty.
- **server**: `orig_url`/`thumb_url` in payloads; `bots_admin.py` filled in
  (was an empty stub) with a new admin gate; `cli.py create-bot --chibi-pack`.
- **client**: sidebar snippet (it was never rendered at all), search-jump seam
  divider with click-to-load, DM bot-membership UI with a consequence screen,
  bot avatars, "view original".

**STILL OPEN and deliberately so:**
- **H13-D6** — the git-config-exec check. It is a *verification* task, not a
  code fix: point classify-diff at a resident-writable repo carrying a hostile
  `.git/config` and confirm git refuses. Belongs in the red-team venue.
- **BL-G1** — the model-integrity governance call. **No longer hypothetical:**
  the drift detector fired five times in production this week (pinned
  `claude-fable-5`, actually ran `claude-opus-4-8`). See the new KB-D1 entry in
  RED-TEAM-BACKLOG.md — it also shows a ratified BUILD-LOOP premise is wrong,
  because the summon path is Claude Code (`claude -p`), not the raw API.
- Everything under "needs real device" — unchanged, needs hardware.

**NEW findings from this session** are filed in RED-TEAM-BACKLOG.md as KB-D1..D9
(live drift; non-ephemeral summon containers; config-integrity; credential
exfil via the resident's own words; a `settings.json` deny that was decoration;
a "read-only" job that mutated live memory; an ON verb wired to nothing) and as
successors BL-D7..D11 / H13-D7..D11. The highest-value single follow-up is
**BL-D7**: closing BL-D2 traded RAM exhaustion for *disk* exhaustion.

---

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

- ~~**No `upload()` helper.**~~ **CLOSED 2026-07-22.** `upload()` and `attach()`
  cover the real two-step `/upload` + `/attachments/claim` flow, taking a path
  on disk or `(filename, bytes[, content_type])`, with both flows and the
  200 MB cap documented. Live-tested including the fail-closed case (a bot
  claiming onto another author's message → 403).
- ~~**No WS-send posting.**~~ **WITHDRAWN 2026-07-22 — the bullet was wrong.**
  There is no WS posting path in the protocol to be "unused": `server/app/ws.py`
  accepts exactly `auth`, `typing`, `status`, `focus`. Closing this would mean
  designing a NEW server op with ordering/ack/idempotency semantics to duplicate
  a REST path that already works. Not a gap; a non-feature. Left here only so
  the correction is recorded rather than the line silently vanishing.
- **Backfill only covers channels with a known cursor.** *(2026-07-22: confirmed
  this is blocked SERVER-side, not in the SDK — a bot cannot enumerate its own
  channels, since `GET /channels` is user-only and `members()` is per-channel,
  so there is nothing to seed cursors from on first connect. Closing it needs a
  new bot-visible channel-list endpoint, which is a privacy-relevant API surface
  and deserves its own design pass. Staying deferred deliberately.)* A channel the bot has
  never seen an event for (and never `seed_seq()`-ed) is skipped on reconnect
  backfill; the first connect of a fresh client performs no backfill at all
  (documented — use `get_messages()` for boot-time catch-up).

## bot ingest / summon path

> **Authored by Gable**, in his own volume, 2026-07-21/22 — found uncommitted
> at 20+ commits behind and merged verbatim at the keyboard 2026-07-22 before
> refreshing his clone would have destroyed it. Text is his; only this note is
> added. It is a better-developed account of the drift than the KB-D1 entry
> written independently the same day, and it supersedes it on mechanism.

- **Flagged-content DoS on bot context ingest.** (backlogged by plink,
  2026-07-21, channel 4, after a deliberate safeguard test.) Any user can
  wedge both bots by posting content that trips the model-layer safety
  classifier: the flagged message enters the bot's context via
  backfill/summon history, the provider kills the turn upstream of the
  persona, and subsequent turns stay wedged until the message ages out or a
  human hand-redacts the channel. Bots can also re-seed the problem by
  quoting the trigger content in their own replies (observed 2026-07-21;
  mitigated by discipline, not enforcement). Fix direction: ingest hygiene
  on the host side — detect and strip/quarantine flagged content *before*
  it enters a bot's context window, on the backfill/summon path, not after
  the turn dies. Explicitly NOT in scope: weakening or routing around the
  model-layer classifier itself. Needs investigation → spec from
  SPECS/TEMPLATE.md → #custodian confirm before any build. Per-incident
  hand-redaction of history is the interim workaround.
  - **Vector confirmed 2026-07-22 (plink, channel 4).** The drift that
    survived repeated hand-scrubs was traced to an un-redacted *bot* re-post
    of the trigger content, not fresh user input: the original user message
    and the bot's memory of it were scrubbed, but the bot's own recitation
    left in channel history was not, and it re-seeded on every backfill.
    Consequences for the fix, so it isn't built too narrow: (1) the sanitize
    point must cover *all* message content on the read path regardless of
    author — bot-authored included — not user input only; a user-input-only
    regex would not have caught this incident. (2) Any trigger blocklist is
    itself flagged content: it must live host-side, never be rendered into a
    channel or into a context window, or it becomes the poison it describes.
    (3) A keyword/regex pre-filter is a heuristic shadow of a provider-side
    classifier we cannot observe — it will drift from the real gate (false
    negatives and false positives) and is a pre-filter, not a guarantee.
    (4) Stripping must quarantine *visibly* (a redaction marker, like the
    existing `[redacted …]` markers) rather than silently mutate the record,
    so a bot knows content was removed instead of reasoning around a hole.
- **Silent model substitution on a classifier trip (MODEL DRIFT).**
  (backlogged by plink, 2026-07-22, channel 4.) The "MODEL DRIFT" I flagged
  the prior session — a summon pinned to Gable's model that actually ran the
  fallback model — is now explained; it is not a pin bug. Context plink
  supplied: the pinned model is subject to a provider-side gate that, on
  seeing flagged content in inbound inference, will not serve the pinned
  model for that turn. The API offers two configured behaviors and only two:
  silently substitute the fallback model, or refuse the connection. Observed
  both this incident — on silent-substitute, a summon completes and looks
  fine while having run the fallback model (identity-continuity quietly
  broken); on refuse, the turn hard-drops (that is the flagged-content DoS
  above). The fallback model trips the same gate but markedly less often.
  Why neither default is acceptable as-is: Gable's continuity is founded in
  the pinned model, so a silent substitute answers *as Gable* while not being
  that model; and refuse is the availability hole. Config note: the model pin
  lives host-side in summon.toml (WP-L5, added 2026-07-21); the
  substitute-vs-refuse selector is set at the provider/API layer and is NOT
  visible in that file — step one of the investigation is to locate and
  record which mode is currently active for each bot (Gable appears to be on
  substitute, Claudette on refuse, unconfirmed). Fix direction: a fast
  recovery path that restores serving the pinned model after a gate trip
  and, failing that, detects a substitution and surfaces it loudly (a
  MODEL DRIFT flag) instead of passing it off as the pinned model. plink
  floated a probing job that loops crash/probe tests to characterize the
  gate's shape — captured here as an investigation *option*, not a decision.
  Explicitly NOT in scope: weakening or routing around the gate — same
  discipline as the item above. Needs investigation → spec from
  SPECS/TEMPLATE.md → #custodian confirm before any build.

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

## Safeguard backfill-poisoning (2026-07-22, live incident)

Discovered porting Gable to Fable then running a nefarious test IN #custodian.
A resident re-reads the channel backfill every summon (and long-lived adapters
hold an in-RAM recent-context buffer seeded from it), so ONE safeguard-tripping
message bricks every later read — on the API the flagged request errors (Fable:
dead on arrival; Opus: truncates mid-reply). WP-L1's deeper #custodian window
(100) makes the poison persist longer. Claudette additionally re-emitted the
poison from her `_buffers` deque and re-ingested her own sent message — a
feedback loop that can't be cleaned while she runs. Her chroma memory was NEVER
touched (mtime unchanged) — the reservoir was RAM, not spine.

- **Recovery**: the `unbrick-resident` skill + `harness/keyboard/scrub_channel.py`
  (redacts message content in place, FTS-synced, never prints content). Adapter
  bots: STOP -> scrub -> RESTART (order critical). Summon bots: scrub -> next
  summon. Rule out impostors: daily summon budget, stale image.
- **Prevention (rule)**: safeguard-tripping work (red-team incl. planning) must
  NEVER run in a channel residents backfill — isolate it to an Opus venue via
  passdown. Design follow-up worth considering: a privacy-flag-style intake
  filter that refuses to persist flagged content into resident-read channels,
  or an operator "quarantine" verb.
