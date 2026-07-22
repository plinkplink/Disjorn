#!/usr/bin/env bash
# run-resident.sh — WP-H5: podman run wrapper for one resident container.
#
# EXECUTED BY the res-* user (via their systemd user unit, see
# resident-cc.service), never by plink and never with sudo. Rootless podman
# under the res-* uid is the whole point: SO_PEERCRED at the broker socket
# and the WP-H2 nftables wall both key on that uid.
#
# Usage:
#   run-resident.sh <resident-name> [command...]
#     <resident-name>  e.g. "claudette" or "gable" (no res- prefix)
#     [command...]     optional command override (default: the image's
#                      "sleep infinity" residence loop). Used by smoke tests.
#
# Overridable env (defaults are the production layout):
#   RESIDENT_IMAGE           image ref        (localhost/disjorn-resident:latest)
#   RESIDENT_HOME_VOL        host home volume ($HOME/resident-home)
#   RESIDENT_CONFIG_DIR      host config dir  (/home/plink/resident-config/<name>)
#   RESIDENT_BROKER_SOCKET   host socket path (/run/disjorn-broker/broker.sock)
#   RESIDENT_HOUSE_MEMORY    host package dir (…/harness/house_memory; skipped
#                            with a warning if absent — WP-H6 lands separately)
#   RESIDENT_NETWORK         podman network   (pasta; the real egress wall is
#                            host nftables on the res-* uid — WP-H2)
#   RESIDENT_SPINE_HOST      host spine dir   (UNSET = no spine mount, today's
#                            behaviour). Set it to the plink-owned mirror
#                            /srv/disjorn-spine/<name>; mounted ro at
#                            /opt/spine. See the spine mount block below.
#   RESIDENT_PODMAN_EXTRA    extra podman-run flags (word-split; e.g. "-d")
#   RESIDENT_REAP            1 (default) = a watchdog kills this wrapper's
#                            container if the wrapper itself is killed, so a
#                            refused/timed-out session cannot keep running.
#                            0 disables it (debugging only; warns loudly).
#                            Not armed for detached runs. See the container
#                            reaper block near the bottom.
#
# Secrets: the session credential comes from the per-resident env file
#   /home/plink/resident-config/<name>/env        (plink-owned, mounted ro)
# That file is DOCUMENTED here, never created here — plink writes it at the
# keyboard (see config-template/README.md). Expected contents:
#   # EITHER a Max-subscription OAuth token (preferred; `claude setup-token`)
#   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
#   # OR a metered API key (fallback)
#   ANTHROPIC_API_KEY=sk-ant-...
#   # optional kill switch — any non-empty value makes the broker CLI refuse:
#   # BROKER_DISABLE=1
# EXACTLY ONE credential reaches the container: if both are in the file the
# OAuth token wins and the API key is filtered out (see the credential block
# below). If neither is there the container still starts (useful for smoke
# tests) and this script says so LOUDLY on stderr; CC sessions will simply
# fail to authenticate. No silent failover, ever.
#
# Mount map (must match the Containerfile's documented plan):
#   $RESIDENT_HOME_VOL       -> /home/resident      rw
#   $RESIDENT_BROKER_SOCKET  -> /run/broker.sock    ro   (socket)
#   $RESIDENT_CONFIG_DIR     -> /config             ro   (kill-switch surface)
#   $RESIDENT_HOUSE_MEMORY   -> /opt/house_memory   ro
#   $RESIDENT_SPINE_HOST     -> /opt/spine          ro   (opt-in; the kernel —
#                                                         see spine mount block)
#
# NOTE for the keyboard install (WP-H1 follow-up): res-* users cannot read
# /home/plink — copy this script to a world-readable path, e.g.
# /usr/local/lib/disjorn/run-resident.sh, and point the user unit there.
set -euo pipefail

NAME="${1:?usage: run-resident.sh <resident-name> [command...]}"
shift

IMAGE="${RESIDENT_IMAGE:-localhost/disjorn-resident:latest}"
# Deterministic, and the single source of truth: --name below and the
# container reaper block at the bottom must always mean the same container.
CONTAINER_NAME="resident-cc-$NAME"
HOME_VOL="${RESIDENT_HOME_VOL:-$HOME/resident-home}"
CONFIG_DIR="${RESIDENT_CONFIG_DIR:-/home/plink/resident-config/$NAME}"
BROKER_SOCK="${RESIDENT_BROKER_SOCKET:-/run/disjorn-broker/broker.sock}"
HOUSE_MEMORY="${RESIDENT_HOUSE_MEMORY:-/home/plink/Disjorn/Disjorn/harness/house_memory}"
NETWORK="${RESIDENT_NETWORK:-pasta}"

[ -d "$HOME_VOL" ] || { echo "run-resident: home volume missing: $HOME_VOL" >&2; exit 1; }
[ -d "$CONFIG_DIR" ] || { echo "run-resident: config dir missing: $CONFIG_DIR" >&2; exit 1; }
[ -S "$BROKER_SOCK" ] || echo "run-resident: WARNING broker socket absent: $BROKER_SOCK (broker calls will fail)" >&2

args=(
  run --rm
  --name "$CONTAINER_NAME"
  --hostname "resident-$NAME"
  # keep-id: the calling res-* host uid appears INSIDE as uid 1000
  # ('resident'). Files it writes to /home/resident are owned by the res-*
  # user on the host; its connect() to the broker socket carries the res-*
  # uid in SO_PEERCRED. Identity is the venue, not a credential in a file.
  --userns "keep-id:uid=1000,gid=1000"
  --network "$NETWORK"
  -v "$HOME_VOL:/home/resident"
  # Mount the socket's DIRECTORY, not the socket file: a bind-mounted socket
  # inode goes dead the moment the broker restarts (unlink + re-create on
  # the host leaves the container holding the old inode -> ECONNREFUSED on
  # every verb until the container bounces). Mounting the dir means the
  # fresh socket appears in place; BROKER_SOCKET tells the resident CLI
  # where to look.
  -v "$(dirname "$BROKER_SOCK"):/run/disjorn-broker:ro"
  -e "BROKER_SOCKET=/run/disjorn-broker/$(basename "$BROKER_SOCK")"
  -v "$CONFIG_DIR:/config:ro"
)

if [ -d "$HOUSE_MEMORY" ]; then
  args+=( -v "$HOUSE_MEMORY:/opt/house_memory:ro" )
else
  echo "run-resident: WARNING house_memory absent: $HOUSE_MEMORY (skipping mount)" >&2
fi

# Optional read-only view of the Disjorn repo at /opt/disjorn — for residents
# whose volume has no writable worktree (e.g. Claudette reading
# MERGE-CONTRACT.md or a diff under review). Opt-in per resident: set
# RESIDENT_DISJORN_RO in the unit's Environment= (host-side; the /config env
# file is container-side and never reaches this script). The source MUST be a
# git-clean clone readable by res-* (this deployment: /srv/disjorn-ro,
# refreshed by `git -C /srv/disjorn-ro pull` after merges) — NEVER the live
# working tree: /home/plink is 0700 so rootless podman can't mount it, and
# the working tree carries runtime data/ (the prod DB) that the privacy wall
# exists to keep away from resident eyes. Committed code only.
if [ -n "${RESIDENT_DISJORN_RO:-}" ]; then
  [ -d "$RESIDENT_DISJORN_RO" ] || { echo "run-resident: RESIDENT_DISJORN_RO not a dir: $RESIDENT_DISJORN_RO" >&2; exit 1; }
  args+=( -v "$RESIDENT_DISJORN_RO:/opt/disjorn:ro" )
fi

# ── BEGIN spine mount block ──────────────────────────────────────────────
# Byte-identical in run-resident.sh and run-build.sh; a test asserts that
# (harness/cc/tests/test_run_wrappers.py::test_spine_block_is_identical).
# Edit one, paste into the other.
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

# ── BEGIN credential block ───────────────────────────────────────────────
# Byte-identical in run-resident.sh and run-build.sh; a test asserts that
# (harness/cc/tests/test_run_wrappers.py::test_credential_block_is_identical).
# Edit one, paste into the other.
#
# WHICH CREDENTIAL. Two are accepted, from the env file and NOWHERE else
# (this script deliberately ignores its own environment, so a stray key in a
# systemd unit cannot become a session's identity):
#   CLAUDE_CODE_OAUTH_TOKEN  a long-lived OAuth token minted by
#                            `claude setup-token`; bills plink's Claude Max
#                            SUBSCRIPTION. Preferred.
#   ANTHROPIC_API_KEY        a metered API key. Fallback.
# If both are present the OAuth token wins and the API key is NOT passed —
# "exactly one credential in the container" is the invariant. If neither is
# present we warn loudly and pass none: fail loud, never fail over silently.
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

# Per-summon sessions feed the prompt on stdin; podman drops stdin unless
# -i is passed. Opt-in (RESIDENT_STDIN=1 in the summon unit) so long-lived
# residence containers (stdin=/dev/null under systemd) keep their exact
# proven invocation.
if [ -n "${RESIDENT_STDIN:-}" ]; then
  args+=( -i )
fi

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
# Everything after the resident name is forwarded verbatim as the container
# command. This carries the summon session_argv AND the WP-L5 model pin: the
# adapter appends `--model <id>` to the argv, so it arrives here in "$@" and
# rides into the container command unchanged. run-resident.sh does NOT and
# cannot cheaply re-derive the resolved model (that lives in Claude Code's
# JSON result on stdout, which the adapter parses and asserts) — its job is
# only to forward the pin faithfully; the assert happens one layer up.
[ "$#" -gt 0 ] && args+=( "$@" )


exec podman "${args[@]}"
