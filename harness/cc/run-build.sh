#!/usr/bin/env bash
# run-build.sh — WP-L4: podman run wrapper for one DETACHED build session.
#
# The sibling of run-resident.sh. Same container/user/mount discipline, but for
# a build-from-a-confirmed-spec session rather than a summon: the worktree is
# read-write (the build writes code and commits to a branch), the wall-clock cap
# is longer (a build is a whole feature, not a chat turn — enforced by the
# broker reaper, documented below), and the spec arrives on STDIN.
#
# LAUNCHED BY the disjorn-broker `start-build` verb (harness/broker/brokerd.py),
# which execs this DETACHED (start_new_session) and does not wait; a reaper
# thread feeds the spec on stdin and enforces the cap. Like run-resident.sh this
# script is only the faithful forwarder — it decides no policy. The confirm
# gate, the budget, and the branch name all live in the broker; here we start
# the container and forward the pinned model.
#
# Usage:
#   run-build.sh <resident-name> <slug> [command...]
#     <resident-name>  e.g. "gable" (no res- prefix) — the identity the build
#                      runs as (keep-id; SO_PEERCRED at the broker socket).
#                      HOW that identity is actually acquired (WP-L4's open
#                      fork, closed 2026-07-22): the broker does NOT exec this
#                      script directly. It runs
#                        sudo -n /usr/local/lib/disjorn/disjorn-build-launch run <name> <slug> ...
#                      and that helper does `systemd-run --uid=res-<name>`, so
#                      the uid is set by PID 1 before exec — not by a userspace
#                      privilege drop inside a sudo'd process. That is what
#                      makes keep-id and SO_PEERCRED true rather than aspirational:
#                      run directly by plink, podman would map the container to
#                      uid 1000 and $HOME would resolve to the wrong tree entirely.
#     <slug>           the spec slug; branch is loop/<slug> and the slug KEEPS
#                      its YYYY-MM-DD- prefix (BL-D4), so branch name == spec
#                      basename 1:1. The container name and the transient unit
#                      name deliberately share the `disjorn-build-<slug>` stem.
#                      Broker-validated kebab, safe as an arg.
#     [command...]     the headless CC build-session argv, forwarded verbatim.
#                      Carries the WP-L5 model pin: the broker appends
#                      `--model <id>`, which rides into the container command via
#                      "$@" (identical mechanism to run-resident.sh).
#
# The SPEC (the chat-derived design) is fed to the session on STDIN, never in
# argv — the launcher.py doctrine: argv is config, chat is data.
#
# NO MERGE, NO PROD: the build lands on its branch and waits for a human. The
# egress wall (WP-H2 host nftables on the res-* uid) blocks external git. The
# ONE writable path out is /run/gatehouse — bare repos with no working tree, so
# a push there deploys nothing and merges nothing. Human merges; nothing lands
# itself.
#
# BRANCH B (2026-08-06). This wrapper provisions a TOOL, not a resident. It has
# no broker socket, no house_memory mount, and its own home and config. See
# build-kernel.md for everything the session is told about itself. Both
# remaining deletions have a rationale block below; read those before adding
# either back.
#
# BRANCH B'S THIRD DELETION IS REVERSED (2026-08-12): the spine mount is back,
# for both seats, per SPECS/2026-08-08-gable-build-lane-provisioning.md
# (confirmed by plink, #custodian seq 1008). The reasoning it was removed on
# held for Claudette's spine and not for Gable's, which has declared a build
# seat since the 07-22 seat-split. Mounting is still not the cutover — see the
# spine mount block. The full contract for this seat, all five surfaces, is
# harness/cc/BUILD-SEAT-CONTRACT.md.
#
# Overridable env (defaults are the production layout — mirror run-resident.sh):
#   RESIDENT_IMAGE           image ref        (localhost/disjorn-resident:latest)
#   RESIDENT_HOME_VOL        host build home  ($HOME/build-home) — the BUILD
#                            seat's own home, NOT the resident's. Holds ~/work
#                            (the clones) and ~/.claude/CLAUDE.md (the task
#                            kernel). Mounted RW.
#   RESIDENT_CONFIG_DIR      host config dir  (/home/plink/build-config/<name>)
#                            — the BUILD seat's own settings.json, separate
#                            from the resident's.
#   RESIDENT_BUILD_KERNEL    task kernel file (/usr/local/lib/disjorn/build-kernel.md)
#                            copied to ~/.claude/CLAUDE.md before launch.
#   RESIDENT_GATEHOUSE       bare repo dir    (/var/lib/disjorn-broker/gatehouse)
#                            mounted RW at /run/gatehouse — clone source and
#                            push target, and the only writable path out.
#   RESIDENT_NETWORK         podman network   (pasta; real egress wall is WP-H2)
#   RESIDENT_SPINE_HOST      host spine dir   (UNSET = no spine mount). Set by
#                            disjorn-build-launch to the plink-owned mirror
#                            /srv/disjorn-spine/<name>; mounted ro at
#                            /opt/spine. See the spine mount block below —
#                            mounting is NOT the cutover.
#   RESIDENT_PODMAN_EXTRA    extra podman-run flags (word-split; e.g. "-d")
#   RESIDENT_REAP            1 (default) = a watchdog kills this wrapper's
#                            container if the wrapper itself is killed, so a
#                            refused/timed-out session cannot keep running.
#                            0 disables it (debugging only; warns loudly).
#                            Not armed for detached runs. See the container
#                            reaper block near the bottom.
#
# SECRETS: same mechanism as run-resident.sh — the session credential comes
# from $RESIDENT_CONFIG_DIR/env and nowhere else, never via argv. But NOT the
# same routing. The build seat is Max-only: CLAUDE_CODE_OAUTH_TOKEN (minted by
# `claude setup-token`) and nothing else. If the env file offers only
# ANTHROPIC_API_KEY this wrapper REFUSES TO LAUNCH rather than billing the
# metered key — the credential-routing spec's "no silent key-fallback",
# enforced at the one place a build can spend. See the credential block below,
# config-template/README.md, and BUILD-SEAT-CONTRACT.md § Credentials.
#
# WALL-CLOCK CAP: enforced by the broker reaper (start_build.timeout_sec,
# suggest 3600s), which kills the session and narrates a loud failure at the
# cap. This script does not embed the timeout, so the single source of truth
# stays the broker config.
#
# NOTE for the keyboard install: res-* users cannot read /home/plink — copy this
# script world-readable, e.g. /usr/local/lib/disjorn/run-build.sh, and point
# [start_build].command at it (KEYBOARD-NEXT.md 6b).
set -euo pipefail

NAME="${1:?usage: run-build.sh <resident-name> <slug> [command...]}"
shift
SLUG="${1:?usage: run-build.sh <resident-name> <slug> [command...]}"
shift

IMAGE="${RESIDENT_IMAGE:-localhost/disjorn-resident:latest}"
# Deterministic, and the single source of truth: --name below and the
# container reaper block at the bottom must always mean the same container.
CONTAINER_NAME="disjorn-build-$SLUG"
# SEAT SPLIT AT THE FILESYSTEM (2026-08-06, "branch B"). The build seat used
# to default to the RESIDENT's home volume and the RESIDENT's config dir —
# same `$HOME/resident-home`, same `/srv/disjorn-resident-config/<name>` — so a
# build session booted inside the resident's own house wearing the resident's
# own settings. That is what produced the 2026-08-05 blocked build: the session
# looked for the resident's assembled kernel, found the placeholder that says
# "do not act on substantive tasks", and correctly refused.
#
# The build seat is a TOOL, not the resident. It gets its own home, its own
# config, and its own kernel (build-kernel.md, ~40 lines, no house rules).
# "Is the builder Claudette or a thing Claudette uses" is settled here, in the
# mounts, not in a prompt: a tool that shares the resident's home is still
# wearing her clothes. The 08-12 spine restoration does not touch that — the
# spine arrives read-only, seat-filtered to the operational set, and it is the
# resident's SPINE, not the resident's HOME.
HOME_VOL="${RESIDENT_HOME_VOL:-$HOME/build-home}"
CONFIG_DIR="${RESIDENT_CONFIG_DIR:-/home/plink/build-config/$NAME}"
HOUSE_MEMORY="${RESIDENT_HOUSE_MEMORY:-/home/plink/Disjorn/Disjorn/harness/house_memory}"
NETWORK="${RESIDENT_NETWORK:-pasta}"
# The task kernel: what this session is told about who it is. Copied into the
# build home below, because Claude Code reads ~/.claude/CLAUDE.md and nothing
# in [start_build].session_argv assembles one — the build argv execs claude
# directly, with no bootstrap.py call in front of it (compare
# residency/summon.toml.template, which has one).
#
# STILL TRUE AFTER THE 08-12 SPINE RESTORATION, and this is the seam to watch:
# mounting /opt/spine does not make anything read it. The spine becomes the
# build seat's kernel only when plink sets RESIDENT_SPINE_DIR=/opt/spine in the
# build /config env file AND puts the bootstrap call in session_argv — at which
# point bootstrap.py OVERWRITES this copied file. One or the other is the
# kernel; never both. BUILD-SEAT-CONTRACT.md § Kernel carries the ordering.
BUILD_KERNEL="${RESIDENT_BUILD_KERNEL:-/usr/local/lib/disjorn/build-kernel.md}"
# The bare repos the build clones from and pushes to. Bare on purpose: no
# working tree means a push cannot deploy.
GATEHOUSE="${RESIDENT_GATEHOUSE:-/var/lib/disjorn-broker/gatehouse}"

[ -d "$HOME_VOL" ] || { echo "run-build: build home missing: $HOME_VOL" >&2; exit 1; }
[ -d "$CONFIG_DIR" ] || { echo "run-build: build config dir missing: $CONFIG_DIR" >&2; exit 1; }
[ -f "$BUILD_KERNEL" ] || { echo "run-build: build kernel missing: $BUILD_KERNEL" >&2; exit 1; }
[ -d "$GATEHOUSE" ] || { echo "run-build: gatehouse missing: $GATEHOUSE" >&2; exit 1; }

# ── BEGIN provisioning ───────────────────────────────────────────────────
# THE WRAPPER BUILDS THE GROUND; THE BUILDER STANDS ON IT.
#
# Everything below runs on the HOST as res-<name> (systemd-run set the uid), so
# it writes to the build home as its owner and needs no privilege at all. The
# session that starts afterwards finds clones already made and a branch already
# checked out, and spends none of its context on setup.
#
# This is deliberate division of labour, not convenience. Branch B says the
# builder does ONE thing; a builder that provisions itself has two jobs and a
# second way to fail. It also means a provisioning failure happens HERE — loud,
# before the model is ever invoked — instead of forty seconds into a session
# that then has to reason about whether its own ground is real.
BRANCH="loop/$SLUG"
WORK="$HOME_VOL/work"

# The task kernel. Copied, not mounted: Claude Code reads $HOME/.claude/CLAUDE.md
# and a bind mount there would fight the home volume.
mkdir -p "$HOME_VOL/.claude" "$WORK"
cp -f "$BUILD_KERNEL" "$HOME_VOL/.claude/CLAUDE.md"

# One FRESH clone per gatehouse repo, every run. Not an optimisation target: a
# local clone hardlinks its objects, so even the 80MB repo costs milliseconds
# and almost no disk. Re-cloning deletes a whole class of failure — the stale
# checkout that silently builds against yesterday's tree — and this week has
# already spent two days on exactly that shape (an uninstalled fix, a stale
# container serving old code). A build is a fresh attempt at a spec, never a
# continuation of a previous one's leftovers.
#
# TWO PATHS FOR THE SAME DIRECTORY, and getting this wrong is the whole reason
# the first push probe failed: we clone HERE, on the host, where the gatehouse
# is $GATEHOUSE — but the session pushes from INSIDE, where the same bare repos
# are bind-mounted at /run/gatehouse. So clone from the host path and then
# rewrite origin to the container path. (Same class as the socket-inode bug:
# whoever runs the command supplies THEIR view.)
GATEHOUSE_IN_CONTAINER="/run/gatehouse"
for _repo_path in "$GATEHOUSE"/*.git; do
  _repo="$(basename "$_repo_path" .git)"
  _dest="$WORK/$_repo"
  rm -rf "$_dest"
  git clone --quiet "$_repo_path" "$_dest" || {
    echo "run-build: FAILED to clone $_repo_path -> $_dest" >&2; exit 1; }
  git -C "$_dest" checkout --quiet -b "$BRANCH" || {
    echo "run-build: FAILED to create $BRANCH in $_dest" >&2; exit 1; }
  # Identity for the commits, local to the clone so the image needs no global
  # gitconfig. The author is the SEAT, not the resident: a build is a tool run,
  # and the audit should not read as though Claudette typed it.
  git -C "$_dest" config user.name  "disjorn-build"
  git -C "$_dest" config user.email "build@disjorn.local"
  # Rewrite origin LAST, to the path the session will actually see.
  git -C "$_dest" remote set-url origin "$GATEHOUSE_IN_CONTAINER/$_repo.git"
done
unset _repo_path _repo _dest
# ── END provisioning ─────────────────────────────────────────────────────

args=(
  run --rm
  # Per-build container name so concurrent builds never collide; the slug is
  # broker-validated kebab (branch/argv-safe).
  --name "$CONTAINER_NAME"
  --hostname "build-$SLUG"
  # keep-id: the calling res-* host uid appears INSIDE as uid 1000
  # ('resident'). Files the build writes to /home/resident are owned by the
  # res-* user on the host; its connect() to the broker socket carries the
  # res-* uid in SO_PEERCRED. Identity is the venue, not a credential.
  --userns "keep-id:uid=1000,gid=1000"
  --network "$NETWORK"
  # The worktree, READ-WRITE: the build commits its work to the loop/<slug>
  # branch here. (run-resident.sh mounts the same volume; a summon just does
  # not commit. The rw-ness is the volume's, called out here for the record.)
  -v "$HOME_VOL:/home/resident"
  # NO BROKER SOCKET. Deliberate deletion, 2026-08-06. The build seat used to
  # mount the broker's socket dir, which is how the 2026-08-05 session could
  # see `broker start-build` from inside a build — a build that starts builds,
  # nine slots deep, and (because [start_build].resident is global, BR-1) an
  # audit trail that cannot tell you it was not the resident. A build needs no
  # verb: its worktree is writable, its remote is the gatehouse, and its report
  # is its stdout, which the broker reaper already reads and posts. Removing
  # the mount kills the whole class rather than filtering it.
  -v "$CONFIG_DIR:/config:ro"
  # The gatehouse, READ-WRITE: the one writable path out of this container.
  # Bare repos, so a push deploys nothing and merges nothing.
  -v "$GATEHOUSE:/run/gatehouse"
  # The spec is fed on stdin; podman drops stdin without -i (always, unlike the
  # opt-in in run-resident.sh — a build with no spec is meaningless).
  -i
)

# NO /opt/house_memory. Also a deliberate deletion. The deployed copy is a
# read-only directory that is not a git repo, and mounting it is what led the
# blocked session to conclude the half of its spec that mattered "had nowhere
# to land". Under branch B the builder edits house_memory where it LIVES —
# ~/work/disjorn/harness/house_memory — and installing it is a keyboard step
# after merge, never the build's job.
: "${HOUSE_MEMORY:=}"  # retained only so the launcher's --setenv stays harmless

# The read-only repo mirror at /opt/disjorn. This was MISSING here while
# run-resident.sh has had it since WP-H1 — run-build.sh only ever mentioned
# RESIDENT_DISJORN_RO inside a comment copied from its sibling, so a build
# session had no /opt/disjorn at all. That is not cosmetic: /opt/disjorn is the
# container-side prefix `[residents.<r>.path_map]` maps for classify-diff, so a
# build could not have tier-classified its own diff. Added 2026-07-22.
#
# Same contract as run-resident.sh: the source MUST be a git-clean clone
# readable by res-* (/srv/disjorn-ro, refreshed after merges) and NEVER the
# live working tree — /home/plink is 0700 so rootless podman cannot mount it,
# and the working tree carries runtime data/ including the prod DB, which is a
# privacy wall, not an inconvenience.
if [ -n "${RESIDENT_DISJORN_RO:-}" ]; then
  [ -d "$RESIDENT_DISJORN_RO" ] || { echo "run-build: RESIDENT_DISJORN_RO not a dir: $RESIDENT_DISJORN_RO" >&2; exit 1; }
  args+=( -v "$RESIDENT_DISJORN_RO:/opt/disjorn:ro" )
fi

# ── BEGIN spine mount block ──────────────────────────────────────────────
# Byte-identical in run-resident.sh and run-build.sh; a test asserts that
# (harness/cc/tests/test_run_wrappers.py::test_spine_block_is_identical_in_both_wrappers).
# Edit one, paste into the other.
#
# BOTH SEATS AGAIN as of 2026-08-12, and the history matters because this block
# has already been deleted once. Branch B (2026-08-06) took it out of
# run-build.sh on two grounds: that a build session is a tool rather than a
# resident, and that `assemble_for_seat("build")` raised "no kernel entry
# visible to seat 'build'" against the spine it was tried on. The second ground
# was an observation about CLAUDETTE's spine, every entry of which is
# `seats: [resident]`, generalised into a claim about the seat.
#
# GABLE's spine has carried an explicit build seat since the seat-split
# (SPECS/2026-07-22-gable-spine-ro-cutover-seat-split.md), applied and verified
# live 07-23: resident 7 entries, build 5, stamped `(seat: build)`. His ruling
# as review owner is that baking the operational set for a detached build is
# "correct, not a compromise" — a build has no retrieval loop, so an
# operational entry that is not baked never arrives at all, and "the build seat
# does load-bearing work with walls it's never read."
#
# The mount is restored for both seats by
# SPECS/2026-08-08-gable-build-lane-provisioning.md (confirmed by plink,
# #custodian seq 1008). Branch B's OTHER two deletions from run-build.sh — the
# broker socket and house_memory — STAND; only the spine came back. See
# harness/cc/BUILD-SEAT-CONTRACT.md before touching any of the three.
#
# PROTECTION BY PLACEMENT for the resident's SPINE — the directory
# house_memory/bootstrap.py assembles into ~/.claude/CLAUDE.md at the start
# of EVERY session. The spine is the resident's kernel. AGENTHOOD.md rules
# that a resident's own code and prompt are always Tier 2 (a human reviews
# every change), and bootstrap.py's docstring assumes a spine edit arrives
# "witnessed, merged".
#
# Neither holds if the spine the container loads sits in the resident's own
# read-write home volume: the resident rewrites its kernel directly and the
# next session loads it — no diff for the WP-H4 classifier to tier, no
# #custodian post, no human. The classifier sees SUBMITTED diffs; it cannot
# see a direct write. Only placement can.
#
# So: mount a plink-owned mirror READ-ONLY at /opt/spine, and have
# RESIDENT_SPINE_DIR (read by bootstrap.py, set in the /config env file)
# point there. Three independent walls, none trusting the others:
#   1. host ownership — the mirror is plink:plink 0755/0644 and the res-*
#      uid is neither owner nor group;
#   2. the `:ro` bind — a write is EROFS even if (1) were wrong;
#   3. the refusal below — we will not launch at all if the source is
#      writable by the uid we are running as. That is the check that
#      catches a cutover mis-pointed back at the home volume.
#
# Opt-in per resident, HOST-side, exactly like RESIDENT_DISJORN_RO: set
# RESIDENT_SPINE_HOST in the unit's Environment=. UNSET adds no mount and
# no flag — byte-for-byte today's podman invocation — so shipping this
# cannot regress a live summon. Mounting alone still changes nothing about
# which spine loads; the cutover is a separate deliberate line in the env
# file (config-template/README.md § Spine placement).
#
# MOUNTING IS NOT THE CUTOVER, and for the BUILD seat that has a second edge.
# The build seat's kernel today is the file run-build.sh COPIES to
# ~/.claude/CLAUDE.md (build-kernel.md), and nothing in [start_build].session_argv
# runs bootstrap.py — so a mounted spine sits there unread until plink both
# sets RESIDENT_SPINE_DIR=/opt/spine in the build seat's /config env file AND
# adds the bootstrap call to session_argv. Those two move together, because
# bootstrap.py WRITES ~/.claude/CLAUDE.md and would otherwise overwrite the
# copied task kernel with no one having chosen that. Both are plink's
# deliberate step, not this wrapper's: harness/cc/BUILD-SEAT-CONTRACT.md
# § Kernel carries the ordering.
#
# The source MUST be the res-readable mirror (/srv/disjorn-spine/<name>,
# published by harness/keyboard/06-spine-mirror.sh after plink approves a
# spine change), NEVER the canonical copy under /home/plink: that tree is
# 0700 and rootless podman cannot mount it. Do not "fix" that by loosening
# /home/plink/bots/<name>/spine — that directory is the authorization
# surface itself. Copy outward; never open inward.
if [ -n "${RESIDENT_SPINE_HOST:-}" ]; then
  _spine_tag="$(basename "$0" .sh)"
  [ -d "$RESIDENT_SPINE_HOST" ] || { echo "$_spine_tag: RESIDENT_SPINE_HOST not a dir: $RESIDENT_SPINE_HOST" >&2; exit 1; }
  # Fail CLOSED, not quietly: if this uid can write the spine source, the
  # read-only mount is theatre (the resident can edit the host path
  # directly, outside the container, and the next session loads it). Refuse
  # the launch and say exactly why. `-writable` is access(2) as the calling
  # uid, so it accounts for ownership, group, and ACLs — not just mode bits.
  _spine_writable="$(find "$RESIDENT_SPINE_HOST" -maxdepth 1 -writable -print -quit 2>/dev/null)"
  if [ -n "$_spine_writable" ]; then
    echo "$_spine_tag: REFUSING TO LAUNCH: spine source is WRITABLE by this uid ($(id -un)): $_spine_writable" >&2
    echo "$_spine_tag: the spine is the kernel and must be resident-unwritable. Point RESIDENT_SPINE_HOST at the plink-owned mirror (/srv/disjorn-spine/<name>, see harness/keyboard/06-spine-mirror.sh) — do NOT loosen the canonical spine to make this pass." >&2
    exit 1
  fi
  unset _spine_writable _spine_tag
  args+=( -v "$RESIDENT_SPINE_HOST:/opt/spine:ro" )
fi
# ── END spine mount block ────────────────────────────────────────────────

ENV_FILE="$CONFIG_DIR/env"

# THE ROUTING LINE (see the credential block's own § ROUTING BY SEAT). This is
# the BUILD seat: per the credential-routing spec, build and agent loops run on
# plink's Max account, so there is NO metered fallback here. A build that finds
# only an API key does not quietly bill it — it refuses to launch. Deliberately
# OUTSIDE the byte-identical block below — it is the one credential decision
# that differs by seat, and it differs because of which wrapper launched, never
# because of /config.
_seat_metered_fallback=refuse

# ── BEGIN credential block ───────────────────────────────────────────────
# Byte-identical in run-resident.sh and run-build.sh; a test asserts that
# (harness/cc/tests/test_run_wrappers.py::test_credential_block_is_identical_in_both_wrappers).
# Edit one, paste into the other.
#
# WHICH CREDENTIAL. Two are accepted, from the env file and NOWHERE else
# (this script deliberately ignores its own environment, so a stray key in a
# systemd unit cannot become a session's identity):
#   CLAUDE_CODE_OAUTH_TOKEN  a long-lived OAuth token minted by
#                            `claude setup-token`; bills plink's Claude Max
#                            SUBSCRIPTION. Preferred.
#   ANTHROPIC_API_KEY        a metered API key. Fallback — but only for a seat
#                            allowed to spend it; see § ROUTING BY SEAT below.
# If both are present the OAuth token wins and the API key is NOT passed —
# "exactly one credential in the container" is the invariant. If neither is
# present we warn loudly and pass none: fail loud, never fail over silently.
#
# ROUTING BY SEAT (SPECS/2026-08-05-credential-routing-and-halt-protocol.md §1
# and §3; SPECS/2026-08-08-gable-build-lane-provisioning.md, architecture note
# 3). The routing table is the seat split: conversational seats run on metered
# per-seat API keys, build/agent loops run on plink's Max account. So the
# API-key fallback is NOT universal. Each wrapper sets `_seat_metered_fallback`
# immediately above this block — `allow` for the chat seat, `refuse` for the
# build seat — and a refusing seat that finds ONLY an API key does not launch.
#
# It refuses rather than warning-and-continuing because the halt protocol says
# so in as many words: no silent key-fallback. A loop that quietly fails over
# from Max to the metered key has converted "your account said not now" into
# "spend your API money instead", which is precisely the decision plink
# reserved for himself (seqs 683/697: "I need to look at the account before
# restarting any work that ate the whole budget"). The full fix is the
# broker-side injecting proxy in that spec, where neither credential is inside
# any container at all; until it lands this refusal holds the same line at the
# one place a build can spend.
#
# The one line that differs by seat lives OUTSIDE this block on purpose, so the
# block stays byte-identical and cannot drift between the two wrappers.
#
# HOW IT IS PASSED (and why not the obvious way).
#   * The value NEVER appears in argv. `podman --env VAR=value` would put a
#     credential for plink's whole Claude account into the process table,
#     readable by any process on the host via /proc/*/cmdline. We use the
#     NAME-ONLY form `--env VAR`, which tells podman "take VAR from my own
#     environment" — argv carries the name only.
#   * The remaining env-file vars (BROKER_DISABLE, …) still go through
#     podman's own --env-file parser, so their semantics never drift from
#     podman's. But podman offers no way to drop a var an --env-file sets:
#     `--unsetenv` does not touch env-file vars, and `--env VAR=` only
#     blanks it (verified against podman 5.4.2). So the file podman reads is
#     a FILTERED copy with both credential lines removed. That copy is
#     created 0600, opened on fd 9, and UNLINKED before exec — it holds no
#     credential and does not outlive the launch. podman does not pass extra
#     fds to the container (no --preserve-fds), so fd 9 stops here.
#   * Value parsing matches podman's env-file semantics exactly: everything
#     after the first "=" is taken literally — no quote stripping, no
#     trimming (verified against podman 5.4.2). Do not quote the token.
#     The bare `NAME` (inherit-from-environment) env-file form is NOT
#     supported for credentials; write NAME=value.
#   * The env FILE itself is masked inside the container: /config is mounted
#     ro, and the resident could otherwise just `cat /config/env` and read
#     the credential out of it (settings.json denies Read(//config/env), but
#     Bash(cat:*) and Bash(python3:*) are allowed, so that deny is hygiene,
#     not a wall). We bind /dev/null over /config/env, so the file reads
#     empty from inside. This removes the FILE copy only — it does NOT and
#     cannot hide the credential from the session itself, which necessarily
#     has it in its own environment (/proc/self/environ). See
#     config-template/README.md § Security note.
#     Escape hatch for debugging: RESIDENT_MASK_ENV=0.
_tag="$(basename "$0" .sh)"
_cred_name=""
_cred_value=""
_oauth_value=""
_apikey_value=""

_read_env_var() {  # _read_env_var NAME FILE -> value on stdout (may be empty)
  local _line
  _line="$(grep -E "^[[:space:]]*$1=" "$2" | tail -n1)" || true
  [ -n "$_line" ] && printf '%s' "${_line#*=}"
  return 0
}

if [ -f "$ENV_FILE" ]; then
  _oauth_value="$(_read_env_var CLAUDE_CODE_OAUTH_TOKEN "$ENV_FILE")"
  _apikey_value="$(_read_env_var ANTHROPIC_API_KEY "$ENV_FILE")"

  if [ -n "$_oauth_value" ]; then
    _cred_name="CLAUDE_CODE_OAUTH_TOKEN"
    _cred_value="$_oauth_value"
    if [ -n "$_apikey_value" ]; then
      echo "$_tag: WARNING $ENV_FILE sets BOTH CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY; using CLAUDE_CODE_OAUTH_TOKEN (Max subscription), NOT passing ANTHROPIC_API_KEY into the container" >&2
    fi
  elif [ -n "$_apikey_value" ]; then
    if [ "${_seat_metered_fallback:-allow}" = "refuse" ]; then
      echo "$_tag: REFUSING TO LAUNCH: $ENV_FILE offers ANTHROPIC_API_KEY only, and this seat routes to the Max account — it must never silently spend the metered key. Put a CLAUDE_CODE_OAUTH_TOKEN (\`claude setup-token\`) in $ENV_FILE, or change the route deliberately at the keyboard. No silent key-fallback: SPECS/2026-08-05-credential-routing-and-halt-protocol.md §3." >&2
      exit 1
    fi
    _cred_name="ANTHROPIC_API_KEY"
    _cred_value="$_apikey_value"
  fi

  if [ -n "$_cred_name" ]; then
    echo "$_tag: auth: $_cred_name from $ENV_FILE" >&2
  else
    echo "$_tag: WARNING no credential in $ENV_FILE (expected CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY) — CC sessions will fail to authenticate" >&2
  fi

  # Filtered copy: every line EXCEPT the two credential assignments (and
  # their bare inherit form). Created 0600, then unlinked — podman reads it
  # through the inherited fd.
  _filtered="$(mktemp "${TMPDIR:-/tmp}/${_tag}-env.XXXXXXXX")"
  chmod 0600 "$_filtered"
  grep -vE '^[[:space:]]*(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY)([[:space:]]*=|[[:space:]]*$)' \
    "$ENV_FILE" > "$_filtered" || true
  exec 9<"$_filtered"
  rm -f "$_filtered"
  args+=( --env-file /dev/fd/9 )

  if [ "${RESIDENT_MASK_ENV:-1}" != "0" ]; then
    args+=( -v "/dev/null:/config/env:ro" )
  else
    echo "$_tag: WARNING RESIDENT_MASK_ENV=0 — /config/env is readable from inside the container; the session can read the credential out of the file" >&2
  fi
else
  echo "$_tag: WARNING env file absent: $ENV_FILE (no CLAUDE_CODE_OAUTH_TOKEN, no ANTHROPIC_API_KEY) — CC sessions will fail to authenticate" >&2
fi

# Hand the winner to podman by NAME only. Unset the loser in our own env so
# nothing inherited can shadow the decision made above.
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY
if [ -n "$_cred_name" ]; then
  export "$_cred_name=$_cred_value"
  args+=( --env "$_cred_name" )
fi
unset _cred_value _oauth_value _apikey_value
# ── END credential block ─────────────────────────────────────────────────

# SEAT (spec 2026-07-22 seat-split). This wrapper is the BUILD seat: a
# detached build session loads the OPERATIONAL set only (00-nonnegotiables,
# 10-people, 20-load-bearing-walls, 30-build-rhythm, 40-cautions) and NEVER
# biography (05-bearings, 50-genesis). "House knowledge travels, biography
# doesn't." bootstrap.py reads RESIDENT_SEAT inside the container and, for the
# build seat, BAKES every entry the seat loads — a detached build has no
# retrieval loop, so an un-baked operational entry would simply be absent.
# Passed AFTER the credential block so the wrapper's seat wins over any
# /config env-file value: the seat is a property of WHICH wrapper launched.
# Deliberately NOT inside a byte-identical block — one of the two lines that
# MUST differ from run-resident.sh (the other is _seat_metered_fallback).
#
# It is passed unconditionally and always has been, INCLUDING while the spine
# was deleted from this seat: RESIDENT_SEAT is the seat's name, not a claim
# that a spine is loaded. It only does anything when bootstrap.py runs, which
# [start_build].session_argv does not currently call — see the BUILD_KERNEL
# note near the top and BUILD-SEAT-CONTRACT.md § Kernel.
args+=( -e "RESIDENT_SEAT=build" )

if [ -n "${RESIDENT_PODMAN_EXTRA:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of extra flags
  args+=( ${RESIDENT_PODMAN_EXTRA} )
fi

# ── BEGIN container reaper block ─────────────────────────────────────────
# Byte-identical in run-resident.sh and run-build.sh; a test asserts that
# (harness/cc/tests/test_run_wrappers.py::test_reaper_block_is_identical).
# Edit one, paste into the other.
#
# THE GAP THIS CLOSES. The container is NOT this process's child. Rootless
# `podman run` hands it to conmon, which is reparented away, so killing the
# podman CLIENT leaves the container running. Measured on podman 5.4.2:
# SIGKILL the client and `podman ps` still shows the container Up
# (tests/test_container.sh check 14a asserts that baseline, so if a future
# podman fixes it upstream we find out instead of quietly duplicating it).
#
# That matters because two supervisors kill this wrapper and expect the
# session to stop with it:
#   * residency/launcher.py's pre-act model gate, when the resolved model
#     does not match the pin — it refuses the session and kills the process
#     it spawned;
#   * brokerd.py's build reaper, at start_build.timeout_sec.
# Their channel guarantee holds regardless (nothing a refused or timed-out
# session produces is ever read or posted). What did NOT stop were the SIDE
# EFFECTS: a refused session inside a still-running container keeps writing
# to the home volume and keeps calling the broker. At the `init` stage that
# window is near-zero, but a mid-session refusal can have tool calls already
# in flight and more still to come.
#
# WHY NOT A SIGNAL TRAP. Both supervisors use Python's `proc.kill()`, which
# is SIGKILL, and no trap runs on SIGKILL — a trap-based reaper would look
# closed without being closed. It would also cost us `exec podman`, and that
# exec is load-bearing: same PID, same stdin, same exit status, no extra
# shell between the supervisor and the container.
#
# WHAT WORKS. A watchdog sibling, started before the exec, that waits for
# THIS pid to disappear and then takes the container down. `$$` survives
# `exec`, so it watches the podman client itself, and it survives the
# wrapper's death because a single-pid kill does not touch it. It covers
# every exit path — SIGKILL, SIGTERM, SIGINT, crash, and normal completion
# (where the container is already gone and the reap is a no-op).
#
# IT REAPS BY CONTAINER ID, NOT BY NAME, and that distinction is the whole
# correctness of this block. Container names are per-resident and REUSED
# every summon ("resident-cc-gable"). A watchdog that reaped by name would,
# in the up-to-one-poll window after its own wrapper exits, kill the NEXT
# summon's container instead of its own — turning a safety feature into an
# intermittent killer of healthy sessions. (This is not hypothetical: the
# first version of this block did exactly that, and check 14 caught it.)
# --cidfile pins the identity, so the watchdog can only ever reap the one
# container it was started for.
#
# `rm -f -t 0`, chosen deliberately over `stop`:
#   * -t 0 => SIGKILL now, no grace period. A grace period is time in which
#     a session we have already decided to refuse keeps calling tools.
#     Nothing in-container needs flushing: /home/resident is a bind mount,
#     so completed writes are already on the host, and the half-finished
#     work is exactly what must not complete.
#   * --ignore => a container that already exited is not an error, so the
#     watchdog can never turn a clean run into a failure.
#
# NOT ARMED WHEN DETACHED. `RESIDENT_PODMAN_EXTRA=-d` means "start the
# container and return"; the wrapper exiting IS the success path there, and
# a watchdog would kill the container it just started. Detached callers own
# their container's lifetime.
#
# The watchdog's stdio goes to /dev/null: it must not hold the wrapper's
# stdout/stderr pipes open, because launcher.py reads those to EOF and an
# inherited pipe would keep EOF from ever arriving.
#
# Escape hatch for debugging a container that dies too fast to inspect:
# RESIDENT_REAP=0 (warns loudly).
_reap_tag="$(basename "$0" .sh)"
_reap_detached=0
for _w in ${RESIDENT_PODMAN_EXTRA:-}; do
  case "$_w" in -d|--detach|--detach=true) _reap_detached=1 ;; esac
done
if [ "${RESIDENT_REAP:-1}" = "0" ]; then
  echo "$_reap_tag: WARNING RESIDENT_REAP=0 — container $CONTAINER_NAME will OUTLIVE this wrapper if the wrapper is killed; a refused or timed-out session keeps running inside it" >&2
elif [ "$_reap_detached" = "1" ]; then
  : # detached by request: the caller owns the container's lifetime
else
  # A private DIRECTORY, not `mktemp -u`: podman refuses to start if the
  # cidfile already exists, so an unlinked-name guess is a race that would
  # turn into a failed summon. mktemp -d cannot collide.
  _reap_ciddir="$(mktemp -d "${TMPDIR:-/tmp}/${_reap_tag}-cid.XXXXXXXX")"
  _reap_cid="$_reap_ciddir/cid"
  args+=( --cidfile "$_reap_cid" )
  _reap_pid=$$
  (
    while kill -0 "$_reap_pid" 2>/dev/null; do sleep 0.25; done
    # The container may still be being created as we die; give the cidfile a
    # moment to appear before concluding there is nothing to reap.
    for _ in $(seq 20); do [ -s "$_reap_cid" ] && break; sleep 0.1; done
    if [ -s "$_reap_cid" ]; then
      podman rm -f -t 0 --ignore "$(cat "$_reap_cid")"
    fi
    rm -rf "$_reap_ciddir"
  ) >/dev/null 2>&1 </dev/null &
fi
unset _reap_tag _reap_detached _w
# ── END container reaper block ───────────────────────────────────────────

args+=( "$IMAGE" )
# Everything after <resident> <slug> is forwarded verbatim as the container
# command. This carries the build session_argv AND the WP-L5 model pin: the
# broker appends `--model <id>` to the argv, so it arrives here in "$@" and
# rides into the container command unchanged — identical to run-resident.sh.
[ "$#" -gt 0 ] && args+=( "$@" )


exec podman "${args[@]}"
