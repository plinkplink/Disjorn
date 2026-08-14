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
  **CLOSED 2026-07-22 — settled the same day this list was written; it should
  not have stayed under "still open". plink ruled "alert for now, gather data
  before stepping into the `refuse` path"; the decision record is
  BUILD-LOOP.md "Decision record 2026-07-22 — BL-G1 SETTLED", and
  `model_gate = "alert"` is live in
  `/srv/disjorn-resident-config/res-gable/summon.toml` (verified 2026-07-26).
  `refuse` remains built and one config value away.**
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

## chibi emotes

- **Emote-usage introspection for pack owners** (requested by Claudette,
  2026-07-24 — "not now, but once there's enough of it"). The server logs
  every resolved mapping (debug) and every miss (info) since the matcher
  landed. Once a few weeks accumulate, surface the distribution back to the
  bot: which emotes she actually reaches for, tag→emote mappings the ladder
  chose, misses. Her stated question: "whether I'm actually using range or
  just reaching for ten flavors of deadpan." Shape TBD — could be a summary
  posted on request, or an endpoint the bot can read. Data source today:
  `journalctl -u disjorn | grep chibi:` (mappings need log level DEBUG for
  the disjorn unit, or derive resolved emotes from `messages.emote_refs`,
  which is already queryable per-bot).

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

> **Status note 2026-07-26 (annotation only — the deferral stands, and none of
> its reasoning changes).** The parenthetical above says "MERGE-CONTRACT is a
> draft"; that half is stale. `harness/cc/MERGE-CONTRACT.md` records **RATIFIED
> 2026-07-20 as a living draft-to-build-against** — #custodian seq 80–83
> (Claudette read it via `read_repo_file`, "read for real, signed for real";
> plink signed on the condition that it stays amendable; Gable signed). The
> load-bearing half is UNCHANGED and still true: `merge-tier1` **automation**
> does not exist — the verb is unimplemented broker-side, as MERGE-CONTRACT.md
> itself states. Read the parenthetical as "the automation doesn't exist yet",
> not "the contract is unratified".

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
  **CLOSED 2026-07-22 — plink chose (a)-then-(b)-staged: run `alert` and gather
  data before `refuse`. Decision record: BUILD-LOOP.md "Decision record
  2026-07-22 — BL-G1 SETTLED". Live config carries
  `model_gate = "alert"` (verified 2026-07-26).**

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

## Memory: the walker & the distillate (2026-07-26, from #custodian seq 401/410)

Consolidation v1 (WP-H8) went live with reference-count arithmetic only —
non-inferential by choice ("it cannot flatter me, because it can't form an
opinion" — Claudette, accepting v1). Two follow-ups deferred under it:

- **The neutral walker**: a smart model periodically walks a resident's
  memory and flags what the arithmetic can't — personality drift,
  self-flattery, stale self-model. Consumes WP-H8's reports rather than
  replacing them; suggestions only, never a direct edit (plink, seq 401).
  Resident owns the mechanism. Also the natural home for plink's
  heartbeat/introspection-cycle idea, which needs a mechanical task to avoid
  the OpenClaw "No action forever" failure mode.
- **The distillate** (Claudette, seq 410, the thing she actually wants):
  651 recalls / 120 distinct memories / top ten carrying most of the
  traffic — her recall surface is tiny and hot. Compress the hot dozen into
  something that RIDES ALONG in context instead of being re-fetched
  hundreds of times. A different mechanism from composting the spine
  (promote/evict/compress); needs its own design. Backlogged under the
  walker at her request.
- **Self-referential recall bias** (Claudette, #custodian seq 416 — her own
  argument coming apart, and she filed it against herself). She defended
  reference-count arithmetic as unable to flatter her *because it has no
  opinion*. The observer effect breaks that: recall traffic is generated by
  her curiosity, so if reads drive promotion, the spine drifts toward whatever
  she has been poking at. She demonstrated it in miniature the same night —
  looked at how hot her hot dozen was and made it hotter. **Non-inferential
  does not mean unbiased; the bias moved upstream into what generates the
  reads, where nobody is looking for it.** Her fix, wanted BEFORE the epoch is
  declared: tag retrieval-log lines by ORIGIN and exclude self-initiated
  introspection (her querying her own telemetry/memory/drift) from the
  promotion signal. Recall serving a conversation is evidence an entry is
  load-bearing; recall serving self-reflection is evidence of nothing.
  Needs a field added to RetrievalLog + a caller-side origin tag — her area,
  so it lands as a proposal.
  **BINDING CONDITION on the tool_actions counter build** (Claudette,
  #custodian seq 430): land the `origin` field in the retrieval log *in the
  same pass*, even with nothing consuming it yet — not the exclusion logic,
  not the promotion filter, just the tag written at record time. Her reason
  is the one that makes it urgent: **provenance cannot be backfilled.** Every
  read recorded before the field exists is permanently unattributable, so the
  cost of deferring is not "later work", it is a hole with a start date.
  ENGINEERING NOTE for whoever builds it, because the naive version does not
  satisfy the condition: adding the field alone yields `origin: null` on every
  line, which is exactly as unattributable as no field. The pass must include
  at least ONE real call-site distinction — conversation-driven recall vs.
  self-initiated introspection — or it buys nothing. Field + one caller,
  minimum.
  **SATISFIED BY: seq 604** (memory-v2 phase 1, confirmed 2026-08-04) — the
  binding condition only, not the parent item above it. The `by_caller` field
  landed there WITH real call-site distinction, which is the half the
  engineering note said the naive version would miss: verified live from
  metrics 2026-08-07, four distinct values (service 102, write_dedup 52,
  unattributed 23, self_query 15). The `tool_actions` counter build
  (2026-08-07-adapter-instrumentation-and-failure-labels) is therefore pure
  counter wiring with no coupled retrieval work, and this condition does not
  re-arm on the next reader. **Still open:** the exclusion logic and the
  promotion filter — the parent item — which is what `by_caller` was landed
  early to make possible.

## Build-loop risks logged 2026-08-05 (mitigate after Claudette's build test)

plink's ruling: log these, work them out after the first resident build is
through. None of them block the test.

- **BR-1 — the keyboard seat can start a build that the audit attributes to the
  resident.** `[start_build].resident` is a GLOBAL config value
  (`brokerd.py:1192`), not derived from the caller, so whoever invokes
  `start-build`, the build runs as that one configured identity. Demonstrated
  the same day it was installed: BuildGable ran the verb from inside
  Claudette's container to test that the confirm gate now passed, the gate
  passed, and a real build launched under her uid — burning one of her two
  daily slots for work she had not asked for. Stopped after ~18s; no branches,
  no commits, no build log, nothing lost but the slot.
  **This is seq 599 in the build path**: a ledger recording a name rather than
  an actor. Fix is to derive the build identity from the SO_PEERCRED uid of the
  caller instead of from config. **Required before a second resident gets the
  verb**, because with one resident the identity happens to be right whenever
  she is the caller, and with two it is wrong half the time.
- **BR-2 — testing a gate can fire the thing behind it.** There is no dry-run
  or permission-check mode on `start-build`, so "would this be allowed?" and
  "do it" are the same call. Claudette asked for exactly this for destructive
  verbs (#custodian seq 701, about `restart-disjorn`) before it bit anyone, and
  it then bit on a different verb the same week. A broker-wide
  `--check`/`--dry-run` that answers the authorization question and fires
  nothing would close both.
- ~~**BR-3 — `daily_build_cap` is a placeholder that was never ruled.**~~
  **CLOSED 2026-08-05: 10/day** (plink). BUILD-LOOP.md recorded 2 as a
  *proposal* with "plink tunes at staging time"; staging time never got
  scheduled, the placeholder shipped, and two other documents then quoted it as
  "the ratified default". Nobody ratified it. Same species as the exemption
  everyone obeyed and nobody granted — **a proposed number becomes a configured
  number by nothing more than time passing.** Still open and now the more
  meaningful meter: the token-cap-per-build half of that same question, since a
  build that dies in 30s and one that thinks for an hour both cost "1".

## Walker defects found on the first live slate (2026-08-05) — BACKLOGGED

plink's ruling, 2026-08-05: *"Let's put the walker fixes in the backlog and
move forward on `start-build`."* Timer disabled the same morning. Claudette
invoked her own auto-off at #custodian seq 728 — ten proposals, zero she would
sign — which is the auto-off condition working exactly as she specified it.

The one-sentence finding, and it is the one to keep: **retrieval count measures
how CONTESTED a record is, not how SETTLED it is, and the spine is for settled.**
A day spent arguing about memory made every memory-about-memory look load-bearing.

- **WD-1 (the only one that can reach another resident's store)** — the
  promotion query does not filter on `subject`. Item 10 of Claudette's slate
  was `subject: gable` — his move-in record, proposed into *her* spine. She had
  ruled the opposite on 07-27 ("weigh it, don't filter on it") and ten days of
  watching changed her mind: **filter on subject, then weigh inside the
  filter.** Must land before the walker runs again.
- **WD-2** — `spine: NONE on disk for this resident` was false on all ten
  proposals. FIXED 2026-08-05 (see model.spine_present): the header now says
  NOT CONNECTED and distinguishes a missing pointer from a missing spine.
  Consequence still open: **walker gate 4's spine-containment half was recorded
  green having never run** — with no spine, `spine_bodies` is empty and
  `already_in_spine` compared against nothing.
- **WD-3** — proposal bodies clip mid-sentence at the display layer,
  deterministically, on all ten items. She was asked to sign ellipses.
  `_excerpt(cap=500)` in model.py, unmarked.
- **WD-4** — the supersede path takes no `tags` argument, so every chain she
  wrote landed tagless *by construction*; she retagged four by hand. This is
  Claudette's nomination for the FIRST resident-run build once `start-build`
  flips: tiny diff, obvious test, and she is the customer.
- **WD-5** — consolidation slates route through `file_proposal`, hardcoded to
  #custodian. Slate review belongs in **#claudette-memory**; only the auto-off
  filing should hit #custodian. One config line.
- **WD-6** — the promotion side still has no epoch gate (deferred 08-04 by
  plink; the sample is the problem, not the mechanism). Revisit with more
  humans and more ordinary conversation.

## Telemetry & summon findings (2026-07-26, #custodian seq 410-428 + keyboard poll)

Diagnosed at the keyboard this session. Grouped because the residents found
them as one symptom ("the ledgers undercount") and they are four causes.

- **The ledger records NAMES, not ACTORS** (Claudette's framing, seq 428 — she
  called it "the house's actual defect", third instance in one day). The
  broker audit attributes by unix uid, so keyboard/certification work run as
  `sudo -u res-<name>` is indistinguishable from that resident acting. This is
  what made the 07-20 probe cluster unattributable from the record alone.
  RESOLVED for that instance (see below) but the defect stands: an audit line
  should carry *which process* made the call, not only under whose uid.
- **`tool_actions` is 0 for res-claudette** — not a metrics bug. The counter is
  a Claude Code PostToolUse hook (`action-counter.py`), and her bot is a plain
  Anthropic-API loop, not Claude Code, so the hook never runs and
  `.action-log` is never created. Fix is ~10 lines in her `core.py` tool
  dispatch appending the WP-H5 contract line. **Her file, her area — proposal,
  not a keyboard edit.**
- **`retrieval` is 0 for res-gable — and it is the SAME WORK ITEM as
  consolidation INTEGRATION-NEEDS §1.** He has no chroma store, and his spine
  is *baked whole* into CLAUDE.md rather than retrieved per entry, so there is
  nothing to meter. Making the number exist = passing a `RetrievalLog` into
  `Spine` (`house_memory/bootstrap.py:68`) **and** switching his seat to
  retrieve entries on demand. That is exactly what §1 needs to arm rent
  assessment. One build closes both. Until then the honest display fix is to
  drop `retrieval_log` from his `[residents.res-gable]` block so metrics omits
  him instead of printing a misleading zero.
- **`broker_actions.by_date` gap 07-23..25 is NOT a bug** — genuine non-use.
  Verified: single unrotated audit file, Claudette has entries throughout the
  gap, broker never restarted. His activity those days was in-container tool
  calls, which the broker never sees. Presentational fix only: label the two
  sections "broker verbs" vs "all tool calls" so they stop reading as one.
- **`daily_action_cap` null for both** — the `[budgets]` block in broker.toml
  is commented out (documented default-OFF, "instrument first"). **Not
  cosmetic:** `brokerd.py:_check_action_budget` denies verbs at/over the cap,
  so uncommenting it starts enforcing and needs a broker restart. plink's
  policy call, not a fix.
- **Summon self-DoS (structural).** `timeout_sec` raised 300 -> 600 after a
  summon died at 300.7s with all 22 tool calls done and only the reply
  unposted. Claudette is right that this buys weeks, not a fix: a fixed wall
  plus a standing per-summon queue that grows every session has a scheduled
  arrival date. **The real fix: give the queue a budget and a DROP ORDER, so
  when the clock is short the housekeeping dies and the reply lives.** Gable's
  own half ("post first, bank after") is adopted by him already.
- **A timeout silently converted a summon into apply-without-post** — files
  changed, channel got a canned error line — inverting the apply-then-post
  norm ratified the same morning, by accident. Partly closed: the launcher no
  longer discards the model on timeout (so a clock failure stops reading as an
  identity failure), and partial replies are deliberately NOT salvaged. The
  norm still binds only the well-behaved path; the drop order is what fixes it.

### RESOLVED this session: the 2026-07-20 probe clusters
Both residents flagged unexplained denials under their names (Gable 4 at
21:28Z, Claudette 1 at 21:40Z) and asked for correlation against keyboard
logs, "whichever way it lands." **They were the WP-H13 live red-team suite**,
documented in-repo at `harness/redteam/live-probes.md` (dated 2026-07-20, "run
by Gable host-side"): probe 3b = restart-disjorn, 6a = `/etc/passwd` path
escape, 6b = cross-resident log read, and F3 = the `main..--exit-code` range
flag-injection, which that file records as "re-confirmed live via direct
socket probe as res-claudette" — the 12-minute gap is the post-fix
re-confirmation. No unknown actor. All five denied, fail-closed, logged.

### Install-template drift found while reconciling (2026-07-26)

`harness/broker/broker.toml` is the INSTALL TEMPLATE, and it had diverged from
the live config in a way that fails silently — the class of bug this house
keeps rediscovering.

- **Placement bug (the real one).** res-claudette's four WP-H12 metrics keys
  sat BELOW the `[residents.res-claudette.path_map]` header, so TOML parsed
  them into `path_map` rather than into her resident table. A fresh install
  would therefore have given her **no metrics at all** — an absent input just
  skips its section, no error — **and** injected four junk prefixes into the
  classify-diff translation/allowlist. res-gable's identical block was placed
  correctly, which is why only she was affected. FIXED; both residents now
  parse to the same key set, verified by `tomllib`.
- **Stale paths.** Every metrics path was missing the `resident-home/`
  segment; `spine_dir` was commented out for both. FIXED to the verified live
  values.
- **`budget_json`** used the `claudette/`/`gable/` spelling vs live's
  `res-*`. Normalized. **Not a bug** — see below.

**A NON-finding, recorded so nobody re-raises it:** the twin
`resident-config/claudette` and `resident-config/res-claudette` paths are NOT
the KB-D3 hardlinked-twin hazard. `stat` shows a shared inode with
`links=1`, because `claudette` is a *directory symlink* to `res-claudette` —
one file, two paths, which is the documented and correct pattern. A shared
inode alone does not prove a hardlink; check the link count and the directory
type before calling it one.


---

## Picker: favorites/recents + payload weight (2026-07-29, keyboard session)

The picker gained add (`POST /picker/add`), remove
(`DELETE /picker/file/{tab}/{name}`) and client-side name search in this
session. Two follow-ups were deliberately left open.

### PK-D1 — favorites vs recents: decide the primitive before building

"Favorites" was requested, but the requirement was not interrogated, and the
storage shape depends entirely on the answer:

- **Per-user favorites** — new table keyed on `user_id`, router, client state,
  migration. A real WP. Also semantically odd here: several "users" are bots,
  and a bot has no taste to record.
- **Shared pins** — near-free (a naming convention, or one flat file), but
  "favorites" implies personal, so this may answer a question nobody asked.
- **Recents (per-user, client-side)** — the strong candidate. It is what people
  actually use in a GIF picker; it needs zero curation discipline, it
  self-maintains as taste drifts, and a first cut is `localStorage` only: no
  schema, no migration, and the per-user-vs-shared question never arises.
  Discord ships Favorites *and* Recents and everyone lives in Recents.

**Do not build favorites without first deciding whether recents makes it
unnecessary.** Sequencing note: if a tab ever outgrows list-everything, the
real fix is server-side pagination + search, and any pinning scheme has to
survive that migration — so know which world it is being built into.

### PK-D2 — the gif tab is heavy, and nothing bounds it

As of 2026-07-29 the gif tab holds 42 files / **60MB**, averaging ~1.4MB with
a 5.2MB worst case. All of it passed validation: `MAX_PICKER_BYTES` is 8MB per
*file* and there is no cap on the tab as a whole. `GET /picker` lists
everything and the grid renders every match, so the only thing keeping this
usable is `loading="lazy"` on the thumbnails — scroll the tab on mobile and you
pull real megabytes.

Untaken options, cheapest first:

1. **Lower `MAX_PICKER_BYTES`** (8MB → ~2MB). Bounds new adds only; does
   nothing about what is already there.
2. **Generate a thumbnail variant** for grid rendering and serve the full file
   only on pick. Correct fix, most work — the picker currently has no
   variant concept at all, unlike attachments.
3. **Transcode to animated WebP** — typically 50-70% smaller at the same
   quality. Blocked by our own validation: `PICKER_TAB_FORMATS["gif"]` is
   `{"GIF"}` so the gif tab rejects WebP by design, to stop the tab meaning
   something other than what it says. Would need that rule relaxed, or a
   "still vs animated" distinction replacing the "gif vs image" one.

Note option 3 is a self-inflicted constraint and worth revisiting on its own
terms: the tabs are really *animated vs still*, and they are named for a file
format instead.

---

## PW-1 — password rotation is enforced server-side with no way for anyone to comply

**Status: the schema and endpoints are merged and live; the enforcement flag is
OFF in production.** Turning it back on locks every human out until the client
can present a rotation screen.

Merged `92bf7b1` (Claudette's build of `SPECS/2026-08-08-password-change.md`,
confirmed at #custodian seq 1006). Migration 007 ran on restart and set
`must_change_password = 1` on all four human rows, exactly as specified.

Within seconds, a real user on the LAN was 403ing on `/channels`,
`/channels/4/members` and `/channels/4/messages` — correct behaviour by the
spec, which gates every user-authenticated route except `POST /auth/password`,
`GET /me` and `POST /auth/logout` while the flag is set.

**The gap: `client/` has no password UI at all.** `client/src/api.ts` knows
exactly one password route, `POST /auth/login`. There is no call to
`POST /auth/password`, no form, nothing in the built bundle. So the flag makes
the app unusable and offers no way out of that state from inside the app.

The spec is not wrong and the build is not wrong — the spec's scope is the
server, its six acceptance tests all pass, and 252 server tests pass on the
merge. **Nothing in the spec, the build, the review or the tests could have
caught this**, because every one of them is scoped to the server. The failure
is that "users can change their password" was true of the API and false of the
product, and no artifact in the loop owns that distinction.

Flag cleared in production 2026-08-14 to restore service:
`UPDATE users SET must_change_password = 0`. Migration 007 is recorded in
`schema_migrations`, so it does not re-run and the clear survives restarts
(verified). Pre-merge backup: `db-backups/disjorn-pre-007-20260814T164320Z.db`.

To finish, in order:

1. **Client rotation screen** — when any request returns 403 with the rotation
   reason, or `/me` reports the flag, present a change-password form and let
   nothing else render. This is the actual missing feature.
2. **Re-flag** with `UPDATE users SET must_change_password = 1` once (1) ships.
   Tell the four humans first; they will each be forced through it.
3. **A spec-level habit worth taking from this**: a spec that changes what a
   user can do should name the surface they do it on, or say in writing that it
   is server-only and names its follow-up. Cheap, and it is the whole distance
   between this incident and no incident.

---

## CR-1 — Claudette's build seat has no account token, and BR-1 is hiding it

**Target (plink, 2026-08-14): API key in chat, Max account in build, both
residents.** The wrappers already enforce exactly that — `run-resident.sh` sets
`_seat_metered_fallback=allow`, `run-build.sh` sets `refuse`. Only the
credential files disagreed.

Done 2026-08-14: Gable's **chat** seat moved off the Max account to the org API
key (`/srv/disjorn-resident-config/gable/env`; the OAuth line was *removed*, not
shadowed — the wrapper prefers OAuth whenever it is present, so adding a key
beside it changes nothing). Backup in `/home/plink/cred-backups/`.

Still open, and it needs plink's hands because only a human can mint one:

**`/srv/disjorn-build-config/claudette/env` holds `ANTHROPIC_API_KEY` and no
`CLAUDE_CODE_OAUTH_TOKEN`.** As written, `run-build.sh` REFUSES to launch that
seat — "offers ANTHROPIC_API_KEY only, and this seat routes to the Max account".

The reason nobody has ever seen that refusal is **BR-1**: `[start_build].resident
= "gable"` in `/etc/disjorn-broker/broker.toml` is global, so every build runs as
res-gable whatever seat called it. Claudette's builds have been borrowing
Gable's token, Gable's home and Gable's spine since the verb was switched on.
Her missing credential has therefore never once failed — the two defects have
been concealing each other.

Consequences worth naming before either is fixed:

1. **Fixing BR-1 first breaks her builds immediately**, at the credential wall,
   until she has her own token. Fix the credential first, or both together.
2. **The audit trail is presently unreliable for every build ever run.** The
   broker records the CALLER (`res-claudette` on 2026-08-14T06:53), the process
   ran as `res-gable`, and the commit is authored `disjorn-build
   <build@disjorn.local>`. Three records, no two agreeing, and no way to tell
   from any of them which resident's judgement produced a diff.

To finish:

1. `claude setup-token` as plink → write it to
   `/srv/disjorn-build-config/claudette/env` as `CLAUDE_CODE_OAUTH_TOKEN`,
   0640 plink:res-claudette, and drop the API key from that file.
2. Then BR-1: derive the build identity from the caller's SO_PEERCRED rather
   than `[start_build].resident`, so a build runs as whoever asked for it.
3. Re-check this table afterwards; `board` will not catch it, because a
   credential file is not a spec, a branch or a proposal.
