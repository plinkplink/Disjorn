#!/usr/bin/env bash
# run-resident.sh: podman run wrapper for one resident container, run as the res-* user (SO_PEERCRED keys on it).
# Usage: run-resident.sh <resident-name> [command...]. The spine and credential blocks are byte-identical in run-build.sh (test-enforced); the reaper blocks differ by contract.
# Res-* users cannot read /home/plink: the unit must point at a world-readable copy.
set -euo pipefail

NAME="${1:?usage: run-resident.sh <resident-name> [command...]}"
shift

IMAGE="${RESIDENT_IMAGE:-localhost/disjorn-resident:latest}"
# --name and the reaper share this name. RESIDENT_CONTAINER_SUFFIX keeps the wake lane apart: --replace would kill the other.
CONTAINER_NAME="resident-cc-$NAME${RESIDENT_CONTAINER_SUFFIX:+-$RESIDENT_CONTAINER_SUFFIX}"
HOME_VOL="${RESIDENT_HOME_VOL:-$HOME/resident-home}"
CONFIG_DIR="${RESIDENT_CONFIG_DIR:-/home/plink/resident-config/$NAME}"
BROKER_SOCK="${RESIDENT_BROKER_SOCKET:-/run/disjorn-broker/broker.sock}"
HOUSE_MEMORY="${RESIDENT_HOUSE_MEMORY:-/home/plink/Disjorn/Disjorn/harness/house_memory}"
# pasta is plumbing, not the wall: egress is the host nftables rule on the res-* uid.
NETWORK="${RESIDENT_NETWORK:-pasta}"

[ -d "$HOME_VOL" ] || { echo "run-resident: home volume missing: $HOME_VOL" >&2; exit 1; }
[ -d "$CONFIG_DIR" ] || { echo "run-resident: config dir missing: $CONFIG_DIR" >&2; exit 1; }
[ -S "$BROKER_SOCK" ] || echo "run-resident: WARNING broker socket absent: $BROKER_SOCK (broker calls will fail)" >&2

args=(
  run --rm
  # --init reaps orphaned children; without it long sessions hit fork EAGAIN.
  --init
  # --replace is load-bearing: without it a stale container blocks every start while the old image serves.
  --replace
  --name "$CONTAINER_NAME"
  --hostname "resident-$NAME"
  # keep-id: SO_PEERCRED at the broker carries the res-* uid.
  --userns "keep-id:uid=1000,gid=1000"
  --network "$NETWORK"
  -v "$HOME_VOL:/home/resident"
  # Mount the socket's DIRECTORY: a bind-mounted socket inode dies when the broker restarts.
  -v "$(dirname "$BROKER_SOCK"):/run/disjorn-broker:ro"
  -e "BROKER_SOCKET=/run/disjorn-broker/$(basename "$BROKER_SOCK")"
  -v "$CONFIG_DIR:/config:ro"
)

if [ -d "$HOUSE_MEMORY" ]; then
  args+=( -v "$HOUSE_MEMORY:/opt/house_memory:ro" )
else
  echo "run-resident: WARNING house_memory absent: $HOUSE_MEMORY (skipping mount)" >&2
fi

# Must be a git-clean res-readable clone, never the live tree, which holds the prod DB.
if [ -n "${RESIDENT_DISJORN_RO:-}" ]; then
  [ -d "$RESIDENT_DISJORN_RO" ] || { echo "run-resident: RESIDENT_DISJORN_RO not a dir: $RESIDENT_DISJORN_RO" >&2; exit 1; }
  args+=( -v "$RESIDENT_DISJORN_RO:/opt/disjorn:ro" )
fi

FILES_RO="${RESIDENT_FILES_RO:-/srv/disjorn-files-ro/res-$NAME}"
FILES_OUTBOX="${RESIDENT_FILES_OUTBOX:-/srv/disjorn-files-outbox/res-$NAME}"
if [ -d "$FILES_RO" ]; then
  args+=( -v "$FILES_RO:/files:ro" )  # the server alone writes the export
fi
if [ -d "$FILES_OUTBOX" ]; then
  args+=( -v "$FILES_OUTBOX:/files-outbox" )
fi

# Writable bare repo, the only write path out of a container. Mount the directory: a resident spans repos.
if [ -n "${RESIDENT_GATEHOUSE:-}" ]; then
  [ -d "$RESIDENT_GATEHOUSE" ] || { echo "run-resident: RESIDENT_GATEHOUSE not a dir: $RESIDENT_GATEHOUSE" >&2; exit 1; }
  if ! compgen -G "$RESIDENT_GATEHOUSE/*.git" >/dev/null; then
    echo "run-resident: RESIDENT_GATEHOUSE holds no *.git repos: $RESIDENT_GATEHOUSE" >&2; exit 1
  fi
  args+=( -v "$RESIDENT_GATEHOUSE:/run/gatehouse" )
fi

# ── BEGIN spine mount block ──
# Mount the spine :ro from a plink-owned mirror, never the home volume: a writable kernel loads unreviewed.
# Never mount the canonical /home/plink spine.
# Build seat: mounting is not the cutover (harness/cc/BUILD-SEAT-CONTRACT.md § Kernel).
# Keep the build seat's mount (SPECS/2026-08-08-gable-build-lane-provisioning.md).
if [ -n "${RESIDENT_SPINE_HOST:-}" ]; then
  _spine_tag="$(basename "$0" .sh)"
  [ -d "$RESIDENT_SPINE_HOST" ] || { echo "$_spine_tag: RESIDENT_SPINE_HOST not a dir: $RESIDENT_SPINE_HOST" >&2; exit 1; }
  # Fail closed: a writable source makes the :ro mount theatre.
  _spine_writable="$(find "$RESIDENT_SPINE_HOST" -maxdepth 1 -writable -print -quit 2>/dev/null)"
  if [ -n "$_spine_writable" ]; then
    echo "$_spine_tag: REFUSING TO LAUNCH: spine source is WRITABLE by this uid ($(id -un)): $_spine_writable" >&2
    echo "$_spine_tag: the spine is the kernel and must be resident-unwritable. Point RESIDENT_SPINE_HOST at the plink-owned mirror (/srv/disjorn-spine/<name>, see harness/keyboard/06-spine-mirror.sh) — do NOT loosen the canonical spine to make this pass." >&2
    exit 1
  fi
  unset _spine_writable _spine_tag
  args+=( -v "$RESIDENT_SPINE_HOST:/opt/spine:ro" )
fi
# ── END spine mount block ──

ENV_FILE="$CONFIG_DIR/env"

# ROUTING: refuse, not allow. Allow would silently fall back to the metered key when the OAuth token expires.
_seat_metered_fallback=refuse

# ── BEGIN credential block ──
# Credentials come only from the env file: OAuth token wins over the API key; neither warns, never fails over.
# Every seat refuses the metered key (SPECS/2026-08-05-credential-routing-and-halt-protocol.md).
# The value stays out of argv: `--env VAR` names it. The env file is filtered on fd 9; podman cannot drop a var.
# Write NAME=value unquoted: the value is literal after the first "=".
# /config/env is masked with /dev/null: this hides the file, not the session environment.
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
    # RESIDENT_METERED_OK is host-side only: a resident-readable knob must not reopen metered spend.
    if [ "${_seat_metered_fallback:-allow}" = "refuse" ] \
        && [ "${RESIDENT_METERED_OK:-0}" != "1" ]; then
      echo "$_tag: REFUSING TO LAUNCH: $ENV_FILE offers ANTHROPIC_API_KEY only, and this seat routes to the Max account — it must never silently spend the metered key. Put a CLAUDE_CODE_OAUTH_TOKEN (\`claude setup-token\`) in $ENV_FILE, or launch with RESIDENT_METERED_OK=1 to spend the key deliberately. No silent key-fallback: SPECS/2026-08-05-credential-routing-and-halt-protocol.md §3." >&2
      exit 1
    fi
    if [ "${_seat_metered_fallback:-allow}" = "refuse" ]; then
      echo "$_tag: METERED OVERRIDE: RESIDENT_METERED_OK=1 — spending ANTHROPIC_API_KEY on a Max-routed seat, by deliberate keyboard choice" >&2
    fi
    _cred_name="ANTHROPIC_API_KEY"
    _cred_value="$_apikey_value"
  fi

  if [ -n "$_cred_name" ]; then
    echo "$_tag: auth: $_cred_name from $ENV_FILE" >&2
  else
    echo "$_tag: WARNING no credential in $ENV_FILE (expected CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY) — CC sessions will fail to authenticate" >&2
  fi

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

# Unset the loser so nothing inherited shadows the decision.
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY
if [ -n "$_cred_name" ]; then
  export "$_cred_name=$_cred_value"
  args+=( --env "$_cred_name" )
fi
unset _cred_value _oauth_value _apikey_value
# ── END credential block ──

# Set after the env file so /config cannot reclassify a summon. Must differ from run-build.sh.
args+=( -e "RESIDENT_SEAT=resident" )
for _stamp in DISJORN_SUMMON_MODEL DISJORN_SUMMONER; do
  if [ -n "${!_stamp:-}" ]; then args+=( -e "$_stamp" ); fi  # by name: the value is the launcher's and stays out of argv
done
unset _stamp

# Summons feed the prompt on stdin, which podman drops without -i.
if [ -n "${RESIDENT_STDIN:-}" ]; then
  args+=( -i )
fi

if [ -n "${RESIDENT_PODMAN_EXTRA:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of extra flags
  args+=( ${RESIDENT_PODMAN_EXTRA} )
fi

# ── BEGIN container reaper block ──
# Killing podman leaves the container running: a sibling watchdog reaps it when this pid exits.
# Supervisors send SIGKILL, which runs no trap: keep `exec podman` and the watchdog a sibling.
# Reap by --cidfile ID, never by name: the next summon reuses the name.
# rm -f -t 0: no grace period, so a refused session cannot keep calling tools.
# Not armed when detached (-d). Its stdio goes to /dev/null.
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
  # mktemp -d, not -u: podman refuses an existing cidfile.
  _reap_ciddir="$(mktemp -d "${TMPDIR:-/tmp}/${_reap_tag}-cid.XXXXXXXX")"
  _reap_cid="$_reap_ciddir/cid"
  args+=( --cidfile "$_reap_cid" )
  _reap_pid=$$
  (
    while kill -0 "$_reap_pid" 2>/dev/null; do sleep 0.25; done
    # Wait briefly for a container still being created.
    for _ in $(seq 20); do [ -s "$_reap_cid" ] && break; sleep 0.1; done
    if [ -s "$_reap_cid" ]; then
      podman rm -f -t 0 --ignore "$(cat "$_reap_cid")"
    fi
    rm -rf "$_reap_ciddir"
  ) >/dev/null 2>&1 </dev/null &
fi
unset _reap_tag _reap_detached _w
# ── END container reaper block ──

args+=( "$IMAGE" )
# Forwarded verbatim, including the adapter's model pin.
[ "$#" -gt 0 ] && args+=( "$@" )


exec podman "${args[@]}"
