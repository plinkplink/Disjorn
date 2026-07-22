# config-template — per-resident /config content (WP-H5)

This directory is the TEMPLATE for `/home/plink/resident-config/<name>/`,
which plink creates at the keyboard, owns, and which run-resident.sh mounts
read-only at `/config` inside the resident's container. It is the
outside-the-container lever: every file here changes resident behavior and
none of them can be edited from inside.

Install per resident (keyboard, plink):

    install -d -m 0755 /home/plink/resident-config/claudette
    cp -r config-template/. /home/plink/resident-config/claudette/
    chmod 0755 /home/plink/resident-config/claudette/hooks/*.py

The config dir (and everything in it except `env`) must be world-readable:
the res-* uid reads it through the ro mount. `env` holds the session
credential — see § The session credential below for its exact ownership,
mode, and why it is masked inside the container.

> PATH NOTE. This template documents `/home/plink/resident-config/<name>/`,
> which is also the wrappers' built-in default. The live deployment
> overrides it via the unit's `Environment=RESIDENT_CONFIG_DIR=`, and uses
> `/srv/disjorn-resident-config/`. In there, `gable` and `claudette` are
> SYMLINKS to `res-gable` and `res-claudette`, so the two spellings that
> appear across `harness/` (`gable-summon.service` says `…/gable`,
> `keyboard/05-gable.sh` says `…/res-gable`) resolve to the same directory —
> writing to either is fine. Still: before writing a credential, read the
> actual `RESIDENT_CONFIG_DIR=` out of the unit you are about to restart. A
> token in the wrong directory produces a "no credential" warning and a
> session that fails to authenticate.
>
> Observed today (`ls -l`, contents not read): both `env` files are already
> `-rw-r----- plink:res-<name>` — exactly the mode this document asks for,
> so step 3 below is a confirmation rather than a change.

## Contents

- `settings.json` — Claude Code settings. Symlinked inside the image as
  `/etc/claude-code/managed-settings.json`, i.e. managed policy the
  resident's own `~/.claude` settings cannot override. Permissions: file
  tools allowed in `/home/resident`, `broker` + dev tooling allowed in
  Bash; deny sudo/su, network clients (curl/wget/nc/socat/ssh/scp/rsync),
  podman/systemctl, WebFetch/WebSearch, and any write into `/config`.
  Honest note: the Bash deny list is hygiene — the real egress wall is host
  nftables keyed on the res-* uid (WP-H2), and the real privilege wall is
  the broker (WP-H3). Hooks are wired here and live in `hooks/`.
- `hooks/pre-tool-use.py` — deterministic PreToolUse gate: blocks tool
  inputs that spell the broker socket path (or the `BROKER_SOCKET` var, or
  the socket's directory), blocks `broker`-looking invocations carrying
  `[[CHAT]]...[[/CHAT]]` channel-text markers (adapter contract, WP-H9/H11),
  enforces wall-clock cap + daily action budget from `budget.json`. It is a
  TRIPWIRE, not a wall: its module docstring lists, by name, both the forms
  it detects and the forms it cannot (name reassembly, indirection through
  a written script, socket-path reassembly that also splits the dirname).
  Read that list before relying on it for anything.
- `hooks/action-counter.py` — PostToolUse: appends one JSON line per tool
  call to `/home/resident/.action-log` (WP-H12 counting; never blocks).
- `hooks/session-start.py` — SessionStart: records session start time,
  prints kernel hash + today's budget status into context.
- `budget.json` — `daily_action_cap`, `wall_clock_cap_min`. plink tunes.
- `CLAUDE.md` — placeholder + kernel assembly contract (WP-H7 writes the
  real kernel to `~/.claude/CLAUDE.md` in the home volume).
- `env` — deliberately NOT in this template and never in the repo. plink
  creates it per resident. Holds the session credential (see below) and the
  resident's `RESIDENT_SPINE_DIR` — the path `bootstrap.py` assembles the
  kernel from, and therefore the single line that decides whether a
  resident can rewrite itself. See § Spine placement.

## The session credential

`run-resident.sh` and `run-build.sh` read the credential from
`$RESIDENT_CONFIG_DIR/env` and from nowhere else — a key sitting in the
systemd unit's `Environment=` is deliberately ignored, so there is exactly
one place to look and exactly one place to revoke. Two names are accepted:

| name | what it is | billing |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | long-lived OAuth token from `claude setup-token` | plink's Claude **Max subscription** |
| `ANTHROPIC_API_KEY`       | metered API key | metered API credit |

Rules the wrappers enforce (tests:
`harness/cc/tests/test_run_wrappers.py`, plus checks 8b–8f in
`harness/cc/tests/test_container.sh` against real podman):

- **OAuth wins.** If both names are set, the OAuth token is used and the
  API key is *not passed into the container at all* — it is stripped from
  what podman reads. Exactly one credential exists inside.
- **Fail loud, never fail over silently.** One credential present → a line
  on stderr naming which one (`run-resident: auth: CLAUDE_CODE_OAUTH_TOKEN
  from …`). Both present → a `WARNING` naming the loser. Neither present,
  or no env file at all → a `WARNING`, and the container starts with no
  credential so the failure is an obvious auth error, not a surprise bill.
- **Never in argv.** The value is handed to podman with the name-only
  `--env VAR` form, so `ps` / `/proc/*/cmdline` never carry it. (`--env
  VAR=value` would.) The other env-file vars still go through podman's own
  `--env-file` parser, reading a filtered copy that is created `0600`,
  opened on fd 9, and unlinked before `exec` — that copy contains no
  credential and does not outlive the launch.
- **The `env` file is masked inside the container.** `/config` is mounted
  read-only, so the session could otherwise just `cat /config/env`. The
  wrappers bind `/dev/null` over `/config/env`, so it reads empty from
  inside. `RESIDENT_MASK_ENV=0` disables the mask (debugging only; it warns).
  Only `env` is masked — anything ELSE you put in the config dir stays
  readable by the session. (`gable-key`, currently `0640 plink:res-gable`
  in gable's config dir, is one such file: whatever is in it, the resident
  can read it.)
- **File syntax is podman's, taken literally.** Everything after the first
  `=` is the value: no quote stripping, no whitespace trimming (verified
  against podman 5.4.2). Do **not** quote the token. Last assignment of a
  name wins. The bare-`NAME` inherit form is not honoured for credentials.

Expected file:

    # exactly one of these two:
    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-REPLACE-ME
    # ANTHROPIC_API_KEY=sk-ant-REPLACE-ME
    # kill switch: uncomment to make the broker CLI refuse everything
    # BROKER_DISABLE=1

### Keyboard steps: cut a resident over to the Max subscription

Run as plink. Substitute the real `RESIDENT_CONFIG_DIR` (see the PATH NOTE
above) and the real resident name for `res-gable` throughout.

**1. Mint the token.** Interactive, needs a browser; must be plink, not a
resident, not an agent:

    claude setup-token

It runs an OAuth flow and yields a long-lived token (they look like
`sk-ant-oat01-…`). *Unverified here:* whether it prints the token to the
terminal or only stores it — this was not run. If it only stores it, take
the value from wherever it says it put it. If the NAS is headless, run this
on a desktop and carry the value across; nothing suggests the token is
machine-bound, but that too is unverified.

**2. Write it into the env file without it touching shell history or argv.**
`read -rs` does not echo and does not put the token on a command line;
`tee` receives it on stdin, not in argv:

    umask 077
    read -rsp 'paste token: ' TOK; echo
    printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$TOK" \
      | sudo tee /srv/disjorn-resident-config/res-gable/env >/dev/null
    unset TOK

That overwrites the file. Re-add any other lines you had (e.g.
`BROKER_DISABLE=1`) afterwards with an editor. **Delete the
`ANTHROPIC_API_KEY` line** — the wrappers would ignore it anyway, and a
metered key left in a file readable by the resident's uid is a credential
you are storing for no reason.

**3. Ownership and mode.** plink owns it; the resident's group can read it
because rootless podman runs *as* `res-gable` and must read the file to
pass it in. Nothing else may read it:

    sudo chown plink:res-gable /srv/disjorn-resident-config/res-gable/env
    sudo chmod 0640            /srv/disjorn-resident-config/res-gable/env
    ls -l                      /srv/disjorn-resident-config/res-gable/env
    # want: -rw-r----- 1 plink res-gable

The containing directory stays `0755` (the res-* uid must traverse it).
`0600 plink:plink` will NOT work: the wrapper runs as `res-gable` and will
report "no credential".

**4. Deploy the updated wrappers.** The credential logic lives in the
scripts, so the world-readable copies must be refreshed. Note that
`/usr/local/lib/disjorn/run-resident.sh` today is the pre-cutover copy, and
`run-build.sh` is **not deployed at all** even though `broker.toml`'s
`[commands] command = ["/usr/local/lib/disjorn/run-build.sh"]` points there:

    sudo install -m 0755 /home/plink/Disjorn/Disjorn/harness/cc/run-resident.sh \
                         /usr/local/lib/disjorn/run-resident.sh
    sudo install -m 0755 /home/plink/Disjorn/Disjorn/harness/cc/run-build.sh \
                         /usr/local/lib/disjorn/run-build.sh

**5. Restart so the next container picks it up.** Env is read at container
start, not per turn:

    sudo systemctl --user -M res-gable@ restart gable-summon

**6. Verify — without printing the token.** The wrapper announces its
choice on stderr; that line is the check:

    sudo -u res-gable env RESIDENT_CONFIG_DIR=/srv/disjorn-resident-config/res-gable \
      bash /usr/local/lib/disjorn/run-resident.sh gable true
    # want on stderr:  run-resident: auth: CLAUDE_CODE_OAUTH_TOKEN from …/env
    # bad:             run-resident: WARNING no credential in …

Then summon the resident in a channel. If it answers, auth works. Confirm
the cutover really happened by checking the Anthropic Console for the
metered API key: usage should stop, and the subscription's rate limits
should be what bites instead.

### Rotation and revocation

- **Rotate:** repeat steps 1–3 and 5. *Unverified:* whether minting a new
  token invalidates the previous one. Assume it does not; revoke explicitly.
- **Revoke:** from plink's Claude account settings (claude.ai), where
  long-lived tokens / connected sessions are managed. *Unverified:* the
  exact location of that control, and whether revocation is immediate.
  **Do this before you depend on it:** mint a throwaway token, find the
  revoke control, revoke it, and confirm a session using it now fails. A
  credential you cannot demonstrably revoke is not a credential you should
  put in a container.
- **Panic path, no revocation needed:** delete the env file (or empty it)
  and restart the unit. The next container starts with no credential and
  says so loudly. That stops *this* resident from using the token; it does
  not un-leak a token already read by a session.
- **Rotate on any suspicion at all** — an unexplained session, a prompt
  injection you did not fully trace, an odd `#custodian` summary, a
  resident that started quoting its own environment. This token is cheap to
  replace and expensive to have loose.

## Security note — the risk delta of an OAuth subscription token

Read this before doing the cutover. It is the honest version.

**What changes.** `ANTHROPIC_API_KEY` is a scoped, metered, individually
revocable key: worst case, someone spends plink's API credit. A
`CLAUDE_CODE_OAUTH_TOKEN` is a credential for plink's Claude **account**.
Whatever that token can do — consume the Max subscription, and whatever
else the account's OAuth scope covers — a holder of the token can do, from
anywhere, until it is revoked. The blast radius of a leak goes from "a
bill" to "plink's account". The container's contents did not change; the
value of what is inside it did.

**What is actually mitigated.**

- *Egress.* Host nftables keyed on the res-* uid (WP-H2,
  `harness/keyboard/03-network.sh`) is default-drop with three holes: TCP
  443 to `api.anthropic.com`, loopback TCP 8399 to the Disjorn server, and
  DNS to the local resolver. A resident cannot POST the token to an
  arbitrary host and cannot `curl` one. Note what that list does *not*
  say — see NOT mitigated (2), (3) and (4).
- *Argv.* The token is never in a command line. Verified: `--env VAR`
  name-only form, and `test_container.sh` check 8d scans every readable
  `/proc/*/cmdline` on the host after a real launch and finds nothing.
  (Verified the hard way — the first version of that check reported a
  phantom leak because the scanner's own argv held the needle.)
- *Logs and audit.* The wrappers print the credential's NAME, never its
  value; `test_run_wrappers.py` asserts the value appears on neither
  stdout nor stderr. The broker's audit log records verbs and args, and no
  verb takes the token.
- *Disk.* Nothing writes it into the image (no build-time secret), nothing
  writes it into the container's writable layer, and the filtered env-file
  copy podman reads contains no credential and is unlinked before exec.
  `test_container.sh` check 8e greps the home volume after a real launch.
- *The file copy.* `/config/env` is masked with `/dev/null` inside the
  container, so `cat /config/env` yields nothing. On the host it is
  `0640 plink:res-<name>` inside a `0700` resident home layout — no other
  resident can read another's.

**What is NOT mitigated. Read this part twice.**

1. **The session itself holds the token.** It has to: Claude Code
   authenticates with it. Any code the resident runs — a Bash call, a
   python one-liner, a subagent — can read it from `/proc/self/environ`.
   The mask on `/config/env` removes a *copy*; it cannot remove the
   original. There is no configuration of this harness in which the
   resident cannot see its own credential.
2. **The resident has a channel to humans, and that is an exfil path.**
   Summon replies and session summaries go to Disjorn channels
   (`#custodian` and wherever it was summoned), and the egress wall
   explicitly permits loopback 8399 to the Disjorn server. A resident that
   printed its token into its reply would have exfiltrated it to everyone
   who reads that channel — nftables entirely satisfied. Nothing in this
   harness scans outbound resident text for a token-shaped string. This is
   the single most realistic leak path and it is open.
3. **The permitted egress endpoint is a two-way channel.** `api.anthropic.com`
   is allowed by design. Text the resident sends there leaves the host.
   That is not exfiltration to a third party, but it is not "the token
   cannot leave the box" either.
4. **DNS is a covert channel.** The wall permits DNS to the local resolver,
   which forwards upstream. That is a low-bandwidth path off the host for
   anything encodable in a query name, and a token is small. It is not
   blocked and it is not monitored here.
5. **`settings.json`'s deny list is not a wall.** It denies
   `Read(//config/env)`, but allows `Bash(cat:*)` and `Bash(python3:*)`.
   The mount-level mask is what makes `/config/env` unreadable; the deny
   rule is decoration. Do not read the deny list as containment.
6. **Whether Claude Code caches the token to disk is unverified.** If a
   version starts writing `~/.claude/.credentials.json`, that lands in the
   persistent home volume (`/home/res-<name>/`, `0700`) and survives the
   `--rm` container. Re-check on every `CLAUDE_CODE_VERSION` bump.
7. **Host compromise ends the discussion.** Anything running as root, or as
   `res-<name>` outside the container, reads the env file directly.
8. **The pre-tool-use hook does not help here at all.** It is a tripwire
   with documented holes and it does not look at credentials.

**Recommendation.** The cutover is reasonable *if* plink accepts point 2:
the resident can speak the token into a channel, and only the resident's
own disposition stops it. Concretely:

- Mint a token **for this purpose only**, so it can be revoked without
  disturbing plink's own `claude` CLI on the host.
- **Prove revocation works before the cutover** (see § Rotation). Do not
  put an unrevocable credential in a container.
- Cut over one resident first (gable, the newer one), watch a few summons
  and one build, then do the other.
- Rotate on any suspicion, and on a schedule regardless.
- Keep the metered API key as the fallback path — the wrappers still
  accept it, so backing out is: edit the env file, restart the unit.
- If any of that reads as too much risk for the saving, the honest answer
  is to stay on `ANTHROPIC_API_KEY` and cap spend at the Console. Nothing
  in the harness needs the subscription; this is a cost decision, and it is
  bought with a strictly larger credential.

**Never commit a token.** This repo carries placeholders only
(`sk-ant-oat01-PLACEHOLDER-…` in tests); `test_run_wrappers.py` asserts the
wrappers contain nothing that looks real.

## Spine placement — where the resident's kernel is allowed to live

The **spine** is a directory of markdown entries that
`house_memory/bootstrap.py` assembles into `~/.claude/CLAUDE.md` at the
start of **every** session. It is the resident's kernel. Claudette's
phrasing, which the house adopted: *"the closest thing I have to a spine,
and edits to it are edits to me."* AGENTHOOD.md puts a resident's own code
and prompt permanently in **Tier 2** — a human reviews every change — and
`bootstrap.py`'s own docstring assumes a spine edit arrives "witnessed,
merged".

### The hole this closes

`bootstrap.py` reads the spine from `RESIDENT_SPINE_DIR`, set in this
directory's `env` file. Gable's says:

    RESIDENT_SPINE_DIR=/home/resident/bots/fable/spine

`/home/resident` is the **read-write home volume**; on the host that path is
`/home/res-gable/resident-home/bots/fable/spine`, `drwxrwxr-x res-gable
res-gable`. Verified from the `res-gable` uid, `find -writable` lists the
directory and all six entries. So the resident can rewrite its own kernel
and the next summon loads it: no diff, no classifier, no `#custodian`, no
human.

The WP-H4 classifier now enumerates `bots/fable/spine` as protected, so a
**submitted diff** touching it is Tier 2. That is the detector half. A
detector cannot see a direct write. **Placement is the wall.**

### The three pieces

1. **The canonical spine** — `/home/plink/bots/fable/spine` (6 entries),
   plink-owned, already resident-unwritable. This is the source of truth
   and it does not move. `/home/plink` is `0700`, so no `res-*` uid can
   traverse it and rootless podman cannot mount it. **Never loosen that to
   make something work.** Copy outward; do not open inward.
2. **The mirror** — `/srv/disjorn-spine/<name>`, `drwxr-xr-x plink plink`,
   entries `-rw-r--r-- plink plink`. Same spirit as `/srv/disjorn-ro` and
   `/srv/disjorn-resident-config`: world-readable, resident-unwritable.
   Published by `harness/keyboard/06-spine-mirror.sh`.
3. **The mount** — `run-resident.sh` and `run-build.sh` mount
   `$RESIDENT_SPINE_HOST` **read-only** at `/opt/spine`, opt-in per resident
   from the systemd unit's `Environment=` (host-side), exactly like
   `RESIDENT_DISJORN_RO`. **Unset = no mount and no flag**: the wrappers'
   podman argv is byte-identical to what it was before this landed, which is
   what both live residents run on today.

### Publish / refresh the mirror

Run as plink, after approving and applying a spine change to the canonical
copy. Idempotent; also prunes entries you deleted upstream:

    sudo bash /home/plink/Disjorn/Disjorn/harness/keyboard/06-spine-mirror.sh gable

It refuses to publish an empty spine, publishes `*.md` only (naming anything
it skips), and asserts nothing it wrote is group- or other-writable.

Verify from the resident uid — run it, do not infer it from mode bits:

    sudo -u res-gable /usr/bin/ls -l /srv/disjorn-spine/gable      # readable
    cd / && sudo -u res-gable /usr/bin/find /srv/disjorn-spine -writable -print
    # want: no output at all

### The cutover — the exact lines plink changes

Two lines, one host-side and one container-side. Neither is applied yet.

**(a) Host-side**, in the resident's systemd user unit
(`/home/res-gable/.config/systemd/user/gable-summon.service`), alongside the
existing `RESIDENT_DISJORN_RO=` line:

    Environment=RESIDENT_SPINE_HOST=/srv/disjorn-spine/gable

**(b) Container-side**, the one-line change in
`/srv/disjorn-resident-config/res-gable/env` — replace

    RESIDENT_SPINE_DIR=/home/resident/bots/fable/spine

with

    RESIDENT_SPINE_DIR=/opt/spine

Then:

    sudo install -m 0755 /home/plink/Disjorn/Disjorn/harness/cc/run-resident.sh \
                         /usr/local/lib/disjorn/run-resident.sh
    sudo install -m 0755 /home/plink/Disjorn/Disjorn/harness/cc/run-build.sh \
                         /usr/local/lib/disjorn/run-build.sh
    sudo systemctl --user -M res-gable@ daemon-reload
    sudo systemctl --user -M res-gable@ restart gable-summon

Verify after the next summon, on the host:

    grep 'assembled from' /home/res-gable/resident-home/.claude/CLAUDE.md
    # want: <!-- assembled from /opt/spine at ... -->

**Rollback** is the same two lines in reverse — drop the
`Environment=RESIDENT_SPINE_HOST=` line, set `RESIDENT_SPINE_DIR` back to
`/home/resident/bots/fable/spine`, `daemon-reload`, restart. The in-volume
copy is left in place precisely so rollback is a config edit and not a data
restore. (It is byte-identical to the canonical spine today — checked.)

Order matters in one direction only: setting (b) without (a) gives a session
whose `RESIDENT_SPINE_DIR` points at a path that does not exist, and
`bootstrap.py` exits 2 with `spine at /opt/spine has no kernel entries` —
loud, and no kernel is assembled. Do (a) first.

Note that **(a) alone changes nothing about which spine loads.** The mount
appears at `/opt/spine`, `bootstrap.py` keeps reading the old path, and the
resident behaves exactly as before. That is deliberate: the mount ships
closed, and the cutover is (b), a single deliberate line.

### What the resident loses, said plainly

After the cutover the resident **cannot iterate its own spine unattended.**
Editing `/home/resident/bots/fable/spine` still succeeds — the home volume
is still writable — but nothing reads it any more, so the edit is inert.
The only route from "the resident wants a spine change" to "the next session
loads it" becomes:

    resident: broker file-proposal  ->  #custodian  ->  plink reads it
      ->  plink edits /home/plink/bots/fable/spine
      ->  plink runs 06-spine-mirror.sh
      ->  next session loads it

That is a real loss of autonomy and it should be named as one, not
described as a bug fix. It is the same bargain AGENTHOOD.md already struck
for a resident's own code: *slow spine, fast hands, nothing about who I am
changing in the dark.* What it buys is that the bargain becomes true rather
than merely agreed to. It is also the *symmetric* half of Claudette's
condition — a human proposing a spine change should land in the resident's
review queue the same way; **that half is not built**, and this document
should not be read as claiming it is.

### Enforced by placement vs. still depending on review

**Enforced by placement** (mechanism; holds against a resident that decides
not to cooperate):

- The spine the container loads is not writable by the resident — three
  independent walls: host ownership (`plink:plink`, the `res-*` uid is
  neither owner nor group), the `:ro` bind mount (a write is `EROFS`), and
  the wrappers' refusal to launch at all if the source is writable by the
  uid they run as. Verified from inside a real container: eight write
  shapes (append, truncate, create, delete, rename, mkdir, symlink, chmod),
  all blocked, host copy unchanged.
- The mirror is only ever written by `06-spine-mirror.sh` running as root
  via plink's sudo. No resident-reachable path writes it.
- The canonical spine stays inside `0700 /home/plink`, unreachable from any
  `res-*` uid and un-mountable by rootless podman.

**Still depends on review** (policy; a careless human defeats it):

- That plink actually reads a `file-proposal` before applying it. The
  classifier tiers the diff; it does not read it for you.
- That plink edits the canonical copy and re-runs the refresh script rather
  than hand-editing `/srv/disjorn-spine/<name>` (which works, and would
  silently drift from the source of truth until the next refresh reverts
  it).
- That `RESIDENT_SPINE_DIR` is not pointed back at a writable path. The
  wrapper catches the specific case where `RESIDENT_SPINE_HOST` is writable;
  it cannot catch `RESIDENT_SPINE_DIR` being set to some other in-volume
  directory, because that value is read inside the container by
  `bootstrap.py`, which trusts it.
- That nothing else in the session prompt reintroduces resident-authored
  identity text. `/opt/spine` governs `~/.claude/CLAUDE.md`; it says nothing
  about `MEMORY.md`, the episodic store, or channel context.

### Second consumer: consolidation

`harness/consolidation/config/gable.toml` documents exactly this mirror as
blocker (2) for activating Gable's consolidation run: his spine is
un-traversable under `/home/plink`. The mirror is the res-readable read-only
spine that job needs — same directory, same permissions:

    [spine]
    dir = "/srv/disjorn-spine/gable"

Consolidation only ever reads the spine, so read-only is correct for it; its
promotions are proposals, and applying one is a plink edit to the canonical
copy followed by a refresh — the same path as any other approved change.

## Kill switches, ranked

1. `systemctl --user -M res-<x>@ stop resident-cc` — the whole residence.
2. `verbs.toml` (broker side, /etc) — per-verb, per-resident. Default OFF.
3. `env`: set `BROKER_DISABLE=1` — all broker calls refuse client-side.
4. `env`: delete the credential line — the next container starts unable to
   authenticate, and says so on stderr. Cheapest way to stop one resident
   spending the subscription without touching the token itself.
5. Revoke the OAuth token at claude.ai — stops every resident holding it,
   and plink's own CLI if he reused his personal token (don't; mint a
   dedicated one).
6. `settings.json` deny rules / hook edits — tool surface. Container
   restart required for env changes; settings/hooks are re-read per session.

**Killing the wrapper now kills the container.** Rootless `podman run` hands
the container to conmon, which is reparented away, so killing the podman
client used to leave the session running — measured on podman 5.4.2, and
asserted as a baseline by `test_container.sh` check 14a. That mattered for
the two things that kill a wrapper on purpose: `residency/launcher.py`'s
pre-act model gate when it **refuses** a session, and `brokerd.py`'s build
reaper at `timeout_sec`. Both use `proc.kill()` (SIGKILL), so a signal trap
could not have covered it; the wrappers instead start a watchdog sibling
that reaps the container **by id** (`--cidfile`) once the wrapper's pid is
gone. Consequence for operators: a refused or timed-out session stops making
tool calls and writing files within ~0.25 s, rather than running to
completion unread. `RESIDENT_REAP=0` disables the watchdog for debugging and
says so loudly; detached runs (`RESIDENT_PODMAN_EXTRA=-d`) are never reaped,
because there the caller owns the container's lifetime.
