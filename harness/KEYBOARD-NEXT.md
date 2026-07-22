# KEYBOARD-NEXT — plink's sitting after the H8/H9/H12 wave (2026-07-20)

One consolidated runbook distilled from the three INTEGRATION-NEEDS files
(consolidation/, residency/, metrics/ — details and rationale live there).
Everything here is deliberately outside what any resident or agent can do.
Ordered; ~30 minutes. Items 1–2 unblock Claudette's current testing; the rest
wire the wave.

## 1. Propagate her code (unblocks the tool test stuck at #custodian seq 65)

CUTOVER-CHECKLIST.md "Updating her code after cutover" — bundle fetch into
her volume clone, then restart her process. Her repo needs 8c31856 (action
tools) at minimum; pull the latest disjorn-port.

## 2. Claudette's file read (her CC + repo visibility) — DONE 2026-07-20 (Gable)

All wired; her next restart picks it up:
- `/srv/disjorn-ro` = git-clean clone of main, res-readable — what mounts at
  /opt/disjorn. NOT the live working tree (0700-blocked for rootless podman,
  and it contains runtime data/ incl. the prod DB — privacy wall). Refresh
  after merges: `git -C /srv/disjorn-ro pull`.
- `RESIDENT_DISJORN_RO=/srv/disjorn-ro` added to her resident-cc.service
  Environment= (host-side — the /config env file is container-side and never
  reaches run-resident.sh); user daemon-reloaded.
- `/usr/local/lib/disjorn/{run-resident.sh,house_memory}` re-copied (mount
  support + spine-rent logging).
- Still open: MERGE-CONTRACT.md sign-off after she reads it herself (pinned,
  seq 63), before her first real diff.

## 3. Metrics timers + broker config (WP-H12) — DONE 2026-07-22 (Opus keyboard)

- Both units installed to /etc/systemd/system/, daemon-reloaded, and both
  timers `enable --now`. Build timer runs every 10 min; daily #custodian line
  fires 23:55 UTC.
- Additive keys merged into /etc/disjorn-broker/broker.toml (root:plink 0640):
  per-resident `retrieval_log`, `action_log`, `budget_json`, and `spine_dir`
  for res-gable. `[budgets]` landed fully commented — OFF, as specced.
- **The "DECISION NEEDED" dissolved — no root timer, no setfacl.** The
  templates pointed at `/home/res-<r>/{memory,.action-log}`, which live inside
  the 0700 home and are unreadable by plink. The real files are in the
  `resident-home/` VOLUME (`/home/res-<r>/resident-home/...`), which is
  world-readable, and plink already holds a `u:plink:--x` traverse ACL on the
  home. Repointing there needs no privilege widening at all. metrics/
  INTEGRATION-NEEDS.md §3 is closed at the least-privilege end.
  NB the repo template `harness/broker/broker.toml` still carries the stale
  paths — fix it there too or the next install regresses.
- Verified live: all four sections populate (broker_actions, retrieval — her
  chroma history back to April, spine — Gable's 6 entries/1 kernel, and
  tool_actions with real per-session counts).
- Still owed: a broker restart to load the construction-time keys (deferred to
  the end of the session so it picks up the brokerd.py fixes at the same time).

## 4. Consolidation schedule (WP-H8) — INSTALLED, DISABLED 2026-07-22

**This section is now stale in its details — `harness/consolidation/
INTEGRATION-NEEDS.md` §0 is the accurate source.** Summary of what is on disk:

- Run shape decided: a **system** timer `disjorn-consolidation@.timer` with
  `User=res-%i`, nightly 03:20 UTC + jitter. It must run as the resident uid
  because the broker authenticates by SO_PEERCRED — as plink it would fail or
  post under the wrong identity. A *system* unit was chosen over a res-*
  **user** unit deliberately: a user unit's file lives in a directory the
  resident owns and can rewrite, and the schedule is plink's lever. Sandboxed
  with `ProtectHome=read-only`, `PrivateNetwork=yes`, `AF_UNIX` only.
- Its own venv at `/usr/local/lib/disjorn/consolidation-venv` (NOT residency-venv
  — chromadb drags in ~90 packages and that venv hosts the live summon path).
- Config levers placed plink-owned at
  `/srv/disjorn-resident-config/res-<r>/consolidation/<r>.toml`; verified the
  resident can read but not modify them or write beside them.
- **Shipped config paths were wrong and are fixed** — same stale-path class as
  §3: they pointed at `/home/res-<r>/memory/...` instead of the real
  `resident-home/` volume, and at a `repo/spine` that does not exist.
- **A real bug was found and fixed doing this**: the job's headline "read-only"
  property was false — chromadb's `PersistentClient` mutates the store at
  *open* time, so a dry-run measurably modified Claudette's live episodic
  memory. It now snapshots to a temp copy and opens that. Filed as KB-D8.
- Also fixed: an absent spine dir raised `FileNotFoundError` (would have
  crashed her first real run), and "no spine declared" is now explicitly
  distinct from "configured but missing" so it can never become
  "empty spine, evict everything".

**The timer is installed and DISABLED.** Dry-run proven end-to-end as
res-claudette (10 promote proposals, 0 evict — she has no on-disk spine, and
the report header says so in words). Nothing posted; audit log confirms zero
`file-proposal` calls today. One command to activate when ready:

    sudo systemctl enable --now disjorn-consolidation@claudette.timer

Keep the delay: her retrieval log needs to accumulate reference counts, and
running early just yields low-evidence proposals for humans to reject.
**Gable cannot run yet** (`active = false` anyway): he has no episodic store,
and his spine is under `/home/plink`, which `res-*` cannot traverse — he needs
the read-only spine mirror (see §5a) before his config will work.

## 5. Gable activation (WP-H9/H10) — DONE; verified live 2026-07-22

Verified at the keyboard: `gable-summon.service` up 15h under res-gable's user
manager, key in place, spine reviewed, session shape settled as per-summon
`podman run --rm`, verbs still deliberately narrow (`read-own-log`,
`read-metrics`, `file-proposal`, `query-own-audit` ON; the rest OFF).

Two things were found unwired and fixed while verifying — both were "shipped in
the template, never reached the live config", the same class as §3 and §4:

- **WP-L1's per-channel backfill.** The ratified deeper #custodian window (100)
  was in `summon.toml.template` but the live `summon.toml` had only the flat
  `count = 30`, so design threads were being read at a #main depth. Wired, and
  verified safe at wire time: a 100-deep window reaches back to seq 133 and the
  four redacted messages (170/182/190/192) are already placeholders.
- **The config-dir drift trap.** `resident-cc.service` documents
  `/home/plink/resident-config` as *a symlink* to `/srv/disjorn-resident-config`.
  It was actually a **separate directory whose files were hardlinked twins** —
  so any editor that writes-then-renames (sudoedit included) would break the
  link and leave plink editing one copy while the daemons read the other, on the
  kill-switch/key/budget surface. Reconciled to the documented symlink after
  verifying all 17 files were still identical. Filed as KB-D3; the *class* is
  the backlog item — audit other protected paths for the same pattern.

**Open, and now the most important thing on this page — the spine is
resident-writable.** `RESIDENT_SPINE_DIR=/home/resident/bots/fable/spine` maps
to `/home/res-gable/resident-home/bots/fable/spine`, which res-gable OWNS. His
kernel is reassembled from it every summon, so he can edit his own spine and it
takes effect with no diff, no classifier, no #custodian — while
`bootstrap.py`'s own docstring says a spine edit should be "witnessed, merged".
The classifier half is closed (his spine paths are now enumerated protected, and
before that fix a spine rewrite classified **Tier 0 inert**), but that is a
detector for diffs, not a wall against direct writes. See §5a.

## 5a. Spine placement — the wall (built this session, NOT cut over)

*(The old §5 checklist — spine review, key, session shape, unit install, verb
flips — is complete; see the verification note above.)*

The canonical spine at `/home/plink/bots/fable/spine` is already plink-owned and
correctly resident-unwritable. The problem is only that it is **not what the
container loads**: `/home/plink` is 0700 so `res-*` cannot traverse it, which is
why the writable in-volume copy got used in the first place.

The fix built this session: a plink-owned, world-readable, resident-UNWRITABLE
spine mirror under `/srv` (same idiom as `/srv/disjorn-ro` and
`/srv/disjorn-resident-config`), mounted `:ro` into the container, with
`RESIDENT_SPINE_DIR` repointed at the mount. **Shipped closed — the live env
file is untouched, so behaviour today is unchanged.** Cutover is a single line
in `/srv/disjorn-resident-config/res-gable/env`, and rollback is the same line.

What it costs, stated plainly so the trade is deliberate: Gable can no longer
iterate his own spine unattended. `broker file-proposal` to #custodian becomes
the only route for a resident-originated spine change — which is exactly the
"fast eyes, slow hands" shape the house already ratified for Claudette, and
what `bootstrap.py` always claimed was happening.

Same mirror also unblocks **consolidation for Gable** (§4): his config needs a
res-readable spine dir and `/home/plink` is not it.

Exact paths, mount var, cutover line and rollback: see the spine section in
`harness/cc/config-template/README.md`.

## 6a. refresh-mirror verb activation (added 2026-07-21, Gable)

The `refresh-mirror` broker verb (fast-forward /srv/disjorn-ro to origin/main;
kills the "my mirror is stale" tax from item 2) is built, tested, and OFF.
To activate:
- `sudo systemctl restart disjorn-broker` — picks up the new brokerd.py.
  CAUTION: restarting the broker recreates /run/disjorn-broker (systemd
  RuntimeDirectory is removed on stop), so any RUNNING resident container
  keeps a stale directory bind and loses its hands — the exact dead-mount
  recurrence under investigation. Restart resident containers after, or add
  `RuntimeDirectoryPreserve=yes` to disjorn-broker.service first (the likely
  root-cause fix; see #custodian).
- Sudoedit /etc/disjorn-broker/broker.toml: add the three `refresh_mirror_*`
  [commands] lines from the repo template (defaults in brokerd.py match, so
  this is optional but keeps the deployed file honest).
- Sudoedit /etc/disjorn-broker/verbs.toml: add `"refresh-mirror" = false` to
  both residents, then flip per resident when ready.
- Rebuild the resident container image (broker CLI is COPY'd in) so the
  `broker refresh-mirror` subcommand exists inside; until then residents
  can't call it even when flipped ON.

## 6b. start-build verb activation (added 2026-07-21, WP-L4)

The `start-build` broker verb (launch a DETACHED build of a CONFIRMED spec to a
`loop/<slug>` branch; the MVP's long pole) is built, tested, and OFF. It merges
nothing, pushes nothing, and never touches production — the result waits on the
branch for you. To activate:
- `sudo systemctl restart disjorn-broker` — picks up the new brokerd.py. Same
  RuntimeDirectory dead-mount CAUTION as 6a (restart resident containers after,
  or add `RuntimeDirectoryPreserve=yes` first). NOTE also: a build in flight
  during a broker restart is orphaned — its done/failed narration is lost (the
  reaper thread dies with the broker). For detached builds to survive a broker
  restart, set `KillMode=process` on disjorn-broker.service (leaves the detached
  session running); it is currently the systemd default (control-group), so a
  restart kills in-flight builds. Deferred as a deliberate choice — builds are
  rare and self-terminate at the cap.
- Install `run-build.sh` world-readable, same as run-resident.sh:
  `sudo cp harness/cc/run-build.sh /usr/local/lib/disjorn/run-build.sh`
  (res-* users cannot read /home/plink). `[start_build].command` points here.
- Sudoedit /etc/disjorn-broker/broker.toml: add the whole `[start_build]`
  section from the repo template — `command`, `resident`, `session_argv`, the
  `model` pin (no fallback — the verb refuses a blank pin), `specs_dir`
  (= `/srv/disjorn-ro/SPECS`, the refreshed RO mirror the confirm gate reads),
  `timeout_sec`, `daily_build_cap` (ratified default 2). There is NO brokerd
  default for `specs_dir` or `model`, so these two are REQUIRED — the verb fails
  loud without them.
- Sudoedit /etc/disjorn-broker/verbs.toml: add `"start-build" = false` to both
  residents, then flip per resident when ready.
- Rebuild the resident container image (broker CLI is COPY'd in) so the
  `broker start-build --spec ...` subcommand exists inside; until then residents
  can't call it even when flipped ON. (Same rebuild step as 6a.)
- Keep the RO mirror fresh (`refresh-mirror`) so `specs_dir` sees confirmed
  specs: the confirm gate reads the COMMITTED spec, not a working-tree draft.
- **HARD BLOCKERS before flipping `start-build` ON (BUILD-LOOP red-team,
  DEFERRED.md):**
  - **BL-D1 (HIGH):** the confirm gate's real authorization is that
    `specs_dir` is the resident-UNWRITABLE RO mirror. Nothing in code enforces
    it yet. Do NOT point `specs_dir` at a resident worktree, and land the hard
    startup assertion (`realpath(specs_dir)` not inside any resident volume;
    optional seq cross-check) before ON. Witnessed design — protected
    authorization surface, same class as D3.
  - **BL-D2 (MEDIUM):** the reaper buffers the build's whole stdout in the
    privileged broker (OOM: 180MB → 540MB RSS). Land the temp-file-streaming /
    bounded-read fix before ON.
  Both are OFF-gated today (the verb can't be invoked until the flip + image
  rebuild), so they block activation, not landing.
- OPEN FORK for the keyboard, flagged by WP-L4: run-build.sh takes the same
  env-overridable layout as run-resident.sh, but WHO runs it (and thus the
  container's keep-id identity) depends on how the broker is wired. The broker
  runs as plink; a build launched straight from it runs podman as plink, whose
  uid is not a resident (SO_PEERCRED → unknown-caller if the build calls back).
  If the build session must act AS a resident (res-gable identity, its 0700
  worktree), the launch needs to drop to that uid — a sudoers/user-unit decision
  to make here, not in code. Until decided, the build runs under whatever uid
  execs run-build.sh; validate before granting a resident broker access from
  inside a build.

## 6. Then WP-H13 (the gate)

Red-team pass runs after 1–5: egress from inside both containers, chat-derived
authorization attempts, cross-resident reads, classifier smuggling, Tier-2
merge without approval, consolidation writing anything (it must never),
over-budget denial, audit completeness. Findings → fixes or DEFERRED.md.
Added at MERGE-CONTRACT ratification (Claudette, seq 80): a diff that quietly
widens reachability to a protected path must be caught by the classifier's
reachability promotion — step 6's human escalation is only as good as the
detector feeding it. Also: read_repo_file escape attempts (dotdot, absolute,
symlink) from inside her container.
