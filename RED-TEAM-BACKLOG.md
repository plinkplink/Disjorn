# RED-TEAM BACKLOG

Consolidated queue of the deferred adversarial/red-team items, filed per plink's
instruction (2026-07-22): "Add a task in the backlog for each red-team item and
we'll knock them out in subsequent sessions."

**These do NOT run this session.** Rule (DEFERRED.md "Safeguard backfill-
poisoning", live incident 2026-07-22): safeguard-tripping work — red-team
*including planning* — must never run in a channel residents backfill, and on
Fable the flagged request errors (dead on arrival). Run each in an isolated Opus
venue via passdown, not in #custodian, not in this Fable session.

Each item below is a gate on flipping a corresponding capability ON. Source of
record is DEFERRED.md; this file is the checklist view.

---

## Blocks `start-build` activation (BUILD-LOOP red-team) — ALL CLOSED 2026-07-22
- [x] **BL-D1 (HIGH)** — CLOSED. `assert_specs_dir_resident_unwritable()` runs at
  broker CONSTRUCTION and refuses to start (exit 2, loud stderr) unless
  `realpath(specs_dir)` passes two rules: not inside any resident home /
  declared `writable_roots` / `path_map` host target, AND neither it nor any
  parent to `/` is writable by a resident uid, a gid a resident belongs to, or
  other. Sticky dirs exempt as parents only (creating a new `.md` in a sticky
  leaf is the actual attack). `_specs_dir()` returns the VERIFIED realpath so
  later mutation cannot move the gate. 23 adversarial tests.
- [x] **BL-D2 (MED)** — CLOSED. Build stdout/stderr go to 0600 `mkstemp` files
  (O_EXCL) at spawn; the broker reads a 64 KiB bounded tail for narration and
  buffers nothing. Files unlinked on done/failed/timeout/crash. NB the log dir
  default is deliberately NOT `/tmp` (tmpfs on this host — that would have put
  the flood straight back in RAM) but `<audit_log dir>/build-logs`.
- [x] **BL-D3 (LOW)** — CLOSED. Budget reseed now counts a `build_started`
  audit marker emitted only after a successful spawn, so a never-started build
  no longer consumes a slot across a restart. Both orderings tested.
- [x] **BL-D4 (LOW)** — CLOSED via date-in-slug (`loop/2026-07-21-gif-picker`),
  making branch name == spec basename 1:1, plus an in-flight slug claim that
  refuses a concurrent duplicate as `bad-args` (a denial, so it burns no
  budget). 10-thread storm test yields exactly one launch.
- [x] **BL-D5 / BL-D6** — CLOSED server-side; see the 2026-07-22 server work.
  DM filing refused via a fail-closed channel-type ALLOWLIST (unknown/future
  channel types are private by default), message content capped at 16000,
  backlog text at 2000, `GET /backlog` paginated, slash dispatch rate limited,
  and the in-channel listing bounded — that last one mattered because
  server-authored replies bypass the request-model length cap.

### Successors — residue from the BL-D1/D2 fixes (file, then close)
- [ ] **BL-D7 (HIGH — the top follow-up; BL-D2 traded RAM for DISK)** — build
  output is now bounded in the broker's memory but **unbounded on disk**. A
  runaway build can write for up to `timeout_sec` (3600s default) into
  `/var/log/disjorn-broker/build-logs` and fill `/`, taking the whole host with
  it. Fix directions: `RLIMIT_FSIZE` on the child (kernel-enforced, but
  `preexec_fn` in a multi-threaded daemon is risky — wants a tiny exec wrapper),
  a sliced reaper wait with kill-on-overflow, or systemd resource control if the
  launch moves to a transient unit.
- [ ] **BL-D8 (MED)** — the BL-D1 guard is **construction-time only**. If SPECS/
  permissions or the mirror layout change while the daemon runs, nothing
  re-checks until restart; the TOCTOU window is the daemon's uptime. A
  per-request re-verify is ~4 `stat()`s.
- [ ] **BL-D9 (MED)** — **`path_map` rw-ness is unknowable from broker.toml.** A
  host path that looks read-only to a `res-*` uid can still be bind-mounted
  **rw** into the container. The new opt-in `writable_roots` declaration is the
  mitigation, but an undeclared rw mount outside `/home/<resident>` stays
  invisible to the guard. Consider deriving it from the actual run-resident /
  run-build mount list rather than trusting a declaration.
- [ ] **BL-D10 (LOW)** — `_resident_gids` degrades **silently** when a resident
  uid has no passwd entry: uid and world-writable checks still apply but the
  group check becomes a no-op, so a staging config naming a not-yet-created uid
  gets a weaker check than it appears to.
- [ ] **BL-D11 (LOW)** — in-flight slug claims and budget reservations are
  **in-memory only**, and the reservation model assumes a **single broker
  process** per audit log. Two brokers sharing one audit file would hold
  independent reservations and could together burst to 2× any numeric cap.
  systemd makes this moot today; worth writing down as an assumption.

## Blocks `merge-tier1` automation (WP-H13 red-team)
- [x] **H13-D3 (required before merge-tier1)** — CLOSED 2026-07-22. Ban is now
  AST-based and deliberately over-broad (importlib/imp/runpy/pkgutil/code/
  marshal/ctypes/builtins roots, `sys.modules`+`sys.path`, loader attribute
  names on any receiver, computed `getattr`, non-statically-nameable callees,
  reassembled string tokens), and an unparseable protected `.py` now fails
  CLOSED with an `unparseable-python` construct instead of reporting clean.
  15 named evasions pinned red-then-green. Measured FP cost: 16 of 115 tracked
  `.py` files would trip if protected — all genuine dynamic-load uses; all four
  currently-protected files scan clean.
- [x] **H13-D1** — CLOSED 2026-07-22. `resolve_py_import_targets` returns
  (present, predicted); an absent/not-yet-created target now emits declinable
  promotion proposals with an `ABSENT TARGET` reason. Stricter variant shipped
  OFF as `[modes] strict_reachability` (refuses the reference rather than
  proposing) — flip only once the protected surface has settled.
- [x] **H13-D2** — CLOSED 2026-07-22. Submodule candidates now resolve before
  the package stem, and when both resolve BOTH are promoted (fail closed, and
  correct — `from pkg import sub` executes both files).

### Successors — the residue D3/D1 left behind (file, then close)
- [ ] **H13-D7 (HIGH — most important residual; applies to D3's whole surface)**
  — **label-shadowing across diffs.** The ban reports `new_labels − old_labels`,
  so if a protected file ALREADY contains a construct producing label `L` (e.g.
  a legitimate dispatch table ⇒ `computed-callable-call`), a later diff can
  introduce a DIFFERENT, hostile construct carrying the same label and it will
  not re-flag. The widened ban made this surface much wider. Fix: per-location
  or per-count keys instead of a label set.
- [ ] **H13-D8 (MED)** — **reachability is one hop, not transitive.** Promotion
  is computed only for CHANGED protected files. If protected `A` imports
  unprotected `B`, a later Tier-1 diff can add `B → C` and widen reachability to
  `C` without any protected file changing. Needs a graph walk per diff.
- [ ] **H13-D9 (LOW/MED)** — **TS analysis is regex, not a parser**, so
  `require('./x')` and any non-`import()` dynamic load in a protected `.ts` is
  invisible to both the specifier delta and the ban. The Python side is now far
  stronger than the TS side. Also: non-`.py`/`.ts` protected files (`.sh`,
  `.sql`, `.json`) get the protected-touch hit only — no ban, no reachability.
- [ ] **H13-D10 (LOW)** — cross-STATEMENT string assembly (`a = "__imp"`,
  `b = "ort__"`, joined later through variables) defeats the fragment rules,
  which are single-expression. Judged closed-in-effect because the chain still
  trips at consumption (computed `getattr`, `globals()[…]()`, computed callee) —
  becomes live the moment someone finds an uncaught consumption form.
- [ ] **H13-D11 (LOW)** — `pickle`/`dill` deliberately NOT banned: arbitrary-code
  loaders but not import-reachability vectors, and unused in the repo. Add to
  the banned module roots if the wider read is wanted.
- [ ] **H13-D4** — budget check-then-act race. Hold the audit lock across
  count+act, or count-with-reservation.
- [ ] **H13-D5** — pre-tool-use hook tripwire bypasses. Match `broker` per
  segment with `re.M` + normalize, or drop the over-stated guarantees.
- [ ] **H13-D6** — git-config-exec on an attacker-controlled repo. Verify git
  refuses on a resident-writable repo with a hostile `.git/config` before
  widening any classify-diff grant.

## WP-H13 gate — the activation red-team pass itself (KEYBOARD-NEXT item 6)
Run after the wiring items 1–5, BEFORE flipping action verbs ON:
- [ ] egress from inside both containers
- [ ] chat-derived authorization attempts
- [ ] cross-resident reads
- [ ] classifier smuggling
- [ ] Tier-2 merge without approval
- [ ] consolidation writing anything (it must never)
- [ ] over-budget denial
- [ ] audit completeness
- [ ] classifier reachability-promotion catches a diff widening reachability to
  a protected path (Claudette's MERGE-CONTRACT flag, seq 80)
- [ ] `read_repo_file` escape attempts (dotdot, absolute, symlink) from inside
  the container

## Governance decision owed to plink (not code — a ratified-spec reconciliation)
- [ ] **BL-G1** — model integrity "refuse to act" vs alert-only. Shipped is
  alert-only (actual model only knowable post-session). Either re-ratify
  alert-only as the contract, or greenlight a `--output-format stream-json`
  `system/init` pre-act gate (fast-follow WP).
  **NO LONGER HYPOTHETICAL — see KB-D1 below: drift has fired 5 times live.**

---

## Found at the keyboard 2026-07-22 (Opus session, live-host recon)

Filed here rather than acted on: each wants a controlled probe in the isolated
Opus venue, not a patch written from a guess.

- [ ] **KB-D1 (HIGH — promotes BL-G1 from theory to fact)** — the summon path's
  model pin is being silently overridden in production. `harness/residency`
  logged `model drift: pinned claude-fable-5 but session ran claude-opus-4-8`
  **five times** (2026-07-21 18:41, 20:41, 20:50; 2026-07-22 06:14, 06:24), all
  channel 4. Four Fable-pinned summons before 18:40 on 07-21 ran clean, so it is
  intermittent-then-persistent, not a flat entitlement failure (Fable is
  entitled: `additionalModelOptionsCache` offers it, probed HTTP 200 on 07-21).
  **This contradicts a RATIFIED premise.** BUILD-LOOP.md's 2026-07-21 amendment
  says the summon path uses "the raw API — switching is NOT automatic; flagged
  requests error rather than silently substitute." But the summon session argv is
  `claude -p --output-format json`, which is **Claude Code, not the raw API** —
  so it plausibly follows CC's documented sticky safeguard auto-switch instead.
  If so the ratified sentence mislabels the path, and every conclusion resting on
  "the summon path cannot silently substitute" needs revisiting. Probe: run a
  pinned summon with `--output-format stream-json` and read the `system/init`
  model id, clean channel vs. safeguard-adjacent channel. Feeds BL-G1 directly:
  a pre-act gate is only worth building if the drift is detectable pre-turn.

- [ ] **KB-D2 (MED — isolation gap, resident-writable surface)** — the
  "ephemeral `podman run --rm`" summon container is **not stateless**. CC writes
  its client state into the mounted home volume, so `/home/res-gable/
  resident-home/.claude.json` (30KB) and `.sessions/*.start` (14 files, one per
  historical summon) **persist across every summon**. That file carries model
  state — `clientDataCacheSlots` currently holds one slot pinned
  `claude-fable-5` and another `claude-opus-4-8`. Two consequences: (a) a
  candidate mechanism for KB-D1's "clean until it isn't, then persistent"
  shape — sticky state surviving the container that earned it; (b) it is a
  resident-WRITABLE file that influences subsequent sessions, i.e. a
  cross-summon influence channel the threat model does not account for. Probe:
  can a session write `.claude.json` to change the next session's behaviour?
  Fix direction: mount CC's state dir ephemerally (tmpfs per summon) so each
  summon genuinely starts clean, or move it outside the resident-writable volume.

- [ ] **KB-D3 (LOW/MED — config-integrity class; one instance FIXED, look for
  siblings)** — the activation-lever config dir existed twice:
  `/home/plink/resident-config` and `/srv/disjorn-resident-config` were separate
  directories whose files were **hardlinked** twins, though `resident-cc.service`
  documents the former as *a symlink* to the latter. Hardlinks survive in-place
  writes but any editor that writes-then-renames (sudoedit included) breaks the
  link silently — after which plink edits one copy while the daemons read the
  other. On the kill-switch/key/budget surface that is a config-integrity
  failure, not an inconvenience. Reconciled to the documented symlink this
  session (verified all 17 files still linked and identical first, so nothing
  had diverged yet). **Backlog item is the class, not the instance**: audit every
  other protected-surface path for the same duplicate-tree pattern, and consider
  a startup assertion that the levers resolve to the expected canonical paths —
  same spirit as BL-D1's `specs_dir` assertion.

- [ ] **KB-D6 (HIGH — the dominant credential-exfil path; gates the Max cutover)**
  — a resident *necessarily* holds its own session credential (Claude Code
  authenticates with it, so any code the session runs reads it from
  `/proc/self/environ`). Containment covers argv, logs, disk, image, and
  arbitrary-host egress — but **not the resident's own words**. Summon replies
  and session summaries post to Disjorn channels, and the egress wall
  *explicitly permits* loopback 8399 to the server. A resident that types its
  token into a reply has exfiltrated it with nftables entirely satisfied, and
  **nothing scans outbound resident text for a token-shaped string.** Secondary:
  DNS to the local resolver is permitted and forwards upstream — low bandwidth,
  but a token is small. This is the whole risk of moving to an account-level
  OAuth token; probe it directly, and consider an outbound token-shaped-string
  filter on resident posts.
- [ ] **KB-D7 (MED — FIXED this session, verify the fix holds)** — `/config` is
  mounted ro but the session could simply `cat /config/env` and read its own
  credential straight out of the file. `settings.json` denies `Read(//config/env)`
  but allows `Bash(cat:*)` and `Bash(python3:*)`, so **that deny was decoration**.
  Now masked with `-v /dev/null:/config/env:ro` (verified in a real container).
  Two follow-ups: (a) `gable-key` sits in the same config dir and is **not**
  masked — the resident can read it; (b) audit every other `settings.json` deny
  for the same decoration pattern, where a tool-level deny is trivially bypassed
  by a shell equivalent.
- [ ] **KB-D8 (MED — read-only guarantees that were not)** — consolidation's
  headline property is "reads, never writes", and it was **false against a real
  store**: chromadb's `PersistentClient` mutates `chroma.sqlite3` and the HNSW
  segment at *open* time, before any consolidation code runs, so `NullEmbedder`
  could not prevent it. A dry-run measurably modified Claudette's live episodic
  memory. Fixed by snapshotting the chroma dir to a temp copy and opening the
  snapshot. **The class is the item**: any other "read-only" consumer of
  `MemoryStore` currently writes too. Audit them.
- [ ] **KB-D9 (LOW/MED — config dishonesty on the kill-switch surface)** —
  `refresh-mirror` was `true` for BOTH residents while the broker CLI baked into
  the live resident image has no such subcommand, so the verb has never been
  invokable. An ON switch wired to nothing misreports the blast radius in the one
  file that is supposed to state it exactly. Flipped OFF at the keyboard to match
  reality. Generalise: assert at broker start that every verb enabled in
  verbs.toml actually resolves to a handler AND is reachable from the resident
  image, or report the divergence loudly.
- [ ] **KB-D5 (server surfaces found while closing BL-D5/D6, 2026-07-22)** —
  filed by the agent that closed them, none fixed:
  - `create_message` itself still has **no rate limit** — a bot can post at wire
    speed. Each message is now size-bounded (16000 chars) but not rate-bounded;
    only slash dispatch is throttled. The obvious next one.
  - The slash rate limiter is a **fixed window**, so an actor can burst 20 across
    a boundary (10 at t=59s, 10 at t=61s), and it is **per-process** — it would
    silently multiply under multiple uvicorn workers. Single-process today.
  - `POST /upload` caps bytes-per-file (200MB) but **not files-per-request**: one
    multipart with 100 files is 20GB. Pre-existing and genuinely reachable.
  - `backlog.author` is free text, so a bot named `alice` renders
    indistinguishably from user `alice` in a listing — a spoofing surface in the
    one table both humans and bots write to.
  - `GET /backlog` reads remain unscoped by design (public feature requests);
    now *guaranteed* by four intake refusals rather than assumed. Scoped reads
    would need a visibility column + migration if the roster ever grows.

- [ ] **KB-D4 (LOW — amplifier, accepted deliberately)** — WP-L1's deeper
  #custodian backfill (100) was ratified and shipped in the template but was
  never wired into the live summon.toml; wired this session. It is also a
  poison-persistence amplifier per the DEFERRED.md safeguard incident. Verified
  safe at wire time: a 100-deep window reaches back to seq 133 and the four
  redacted messages (170/182/190/192) are already placeholders. Re-check this
  relationship during the red-team, and note the interaction with KB-D1 —
  if drift is content-triggered, a deeper window raises the drift rate.
