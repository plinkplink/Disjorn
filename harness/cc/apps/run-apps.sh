#!/usr/bin/env bash
# run-apps.sh: one apps-builder turn, run as res-appsbuilding from inside the transient unit, never as root or plink.
# Usage: run-apps.sh <session> <turn> <app-id>, prompt on stdin. Config arrives as APPS_* env from the launcher.
# THE WALL (SPECS/2026-09-13-apps-gitdir-outside-the-mount.md): the writable surface is the work tree only.
# The git dir is mounted nowhere, ever. A repo under /work let a turn plant core.fsmonitor, which the host then executed.
# The seat holds no house credential: no broker socket, gatehouse, house memory or spine is mounted.
set -euo pipefail

_tag="$(basename "$0" .sh)"
_say() { echo "$_tag: $*" >&2; }
_die() { _say "FATAL: $*"; exit 1; }

SESSION="${1:?usage: run-apps.sh <session> <turn> <app-id>   (prompt on stdin)}"
TURN="${2:?usage: run-apps.sh <session> <turn> <app-id>   (prompt on stdin)}"
APP_ID="${3:?usage: run-apps.sh <session> <turn> <app-id>   (prompt on stdin)}"

# Re-validated here: this script is also runnable by hand.
[[ "$SESSION" =~ ^[1-9][0-9]{0,8}$ ]] || _die "session id: $SESSION"
[[ "$TURN"    =~ ^[1-9][0-9]{0,3}$ ]] || _die "turn: $TURN"
[[ "$APP_ID"  =~ ^[a-z2-7]{12}$    ]] || _die "app id: $APP_ID"
[ -t 0 ] && _die "the prompt arrives on stdin (the launcher feeds it); refusing a terminal"

IMAGE="${APPS_IMAGE:-localhost/disjorn-apps-builder:latest}"
MODEL="${APPS_MODEL:-claude-fable-5-2}"
PONYTAIL_MODE="${APPS_PONYTAIL_MODE:-full}"
RUNNER_COMMAND="${APPS_RUNNER_COMMAND:-[\"claude\",\"-p\",\"--output-format\",\"stream-json\",\"--verbose\"]}"
TURN_MAX_SEC="${APPS_TURN_MAX_SEC:-1800}"
REPO_ROOT="${APPS_REPO_ROOT:-/srv/apps}"
# Host-only git root: never mounted, by this script or any other.
GIT_ROOT="${APPS_GIT_ROOT:-/srv/apps-git}"
TURNS_ROOT="${APPS_TURNS_ROOT:-/srv/apps-turns}"
WWW_ROOT="${APPS_WWW_ROOT:-/srv/apps-www}"
QUARANTINE_ROOT="${APPS_QUARANTINE_ROOT:-/srv/apps-quarantine}"
CONFIG_DIR="${APPS_CONFIG_DIR:-/srv/disjorn-build-config/appsbuilding}"
HARVEST="${APPS_HARVEST:-/usr/local/lib/disjorn/apps_harvest.py}"
NETWORK="${APPS_NETWORK:-pasta}"
PODMAN="${APPS_PODMAN:-podman}"

# The work tree and the host-only repository are named separately; neither derives from the other.
REPO="$REPO_ROOT/$APP_ID"                 # the WORK TREE; this is /work
GIT_DIR_PATH="$GIT_ROOT/$APP_ID.git"      # the REPOSITORY; mounted nowhere
RESULT_DIR="$TURNS_ROOT/$SESSION/$TURN"
PREVIEW_DIR="$WWW_ROOT/$APP_ID/preview"
QUARANTINE_DIR="$QUARANTINE_ROOT/$APP_ID/$TURN"
ENV_FILE="$CONFIG_DIR/env"
CONTAINER_NAME="disjorn-apps-$SESSION-$TURN"

[ -x "$HARVEST" ] || [ -f "$HARVEST" ] || _die "harvest module missing: $HARVEST"

# The result dir is 0644 for the broker; the raw spools are 0600, readable by this seat only.
umask 0022
# Clear a stale result.json first: the broker reads its existence as "turn finished".
# Clear before mkdir, so a stop marker the launcher just dropped is not eaten.
rm -f "$RESULT_DIR/result.json" "$RESULT_DIR/scaffolded" "$RESULT_DIR/stop-requested"
mkdir -p "$RESULT_DIR"
# The prompt is copied into the result dir as the seat, 0600: root never writes there.
PROMPT="$RESULT_DIR/prompt.md"
# The byte bound is the launcher's number, passed in; this head -c is a backstop only.
PROMPT_MAX_BYTES="${APPS_PROMPT_MAX_BYTES:-65536}"
( umask 077 && head -c "$PROMPT_MAX_BYTES" > "$PROMPT" ) || _die "cannot stage the prompt"
[ -s "$PROMPT" ] || _die "empty prompt on stdin"
STARTED_AT_EPOCH="$(date +%s)"
STARTED_AT="$(date -u -Is)"
STDOUT_LOG="$RESULT_DIR/stdout.log"
STDERR_LOG="$RESULT_DIR/stderr.log"

# The harvest's argument file is 0600 JSON, removed on exit. It names the credential file, never its value.
# Values cross into python as environment, never interpolated: a quote in a path must not become code.
_spec="$RESULT_DIR/.harvest-input.json"
_write_harvest_spec() {
  ( umask 0077
    H_REPO="$REPO" H_GITDIR="$GIT_DIR_PATH" H_RC="$1" H_RESULT="$RESULT_DIR" \
    H_KEY="$ENV_FILE" H_PREVIEW="$PREVIEW_DIR" H_TURN="$TURN" \
    H_SESSION="$SESSION" H_APP="$APP_ID" H_STARTED="$STARTED_AT" \
    H_MODEL="$MODEL" H_QUAR="$QUARANTINE_DIR" H_OUT="$STDOUT_LOG" \
    H_ERR="$STDERR_LOG" H_TIMEDOUT="$2" \
    python3 - "$_spec" <<'HARVEST_SPEC_EOF'
import json, os, sys
e = os.environ
json.dump({
    "repo": e["H_REPO"],
    "git_dir": e["H_GITDIR"],
    "exit_code": int(e["H_RC"]),
    "result_dir": e["H_RESULT"],
    "key_file": e["H_KEY"],
    "preview_dir": e["H_PREVIEW"],
    "turn": int(e["H_TURN"]),
    "session": int(e["H_SESSION"]),
    "app_id": e["H_APP"],
    "started_at": e["H_STARTED"],
    "model": e["H_MODEL"],
    "quarantine_dir": e["H_QUAR"],
    "spool_stdout": e["H_OUT"],
    "spool_stderr": e["H_ERR"],
    "timed_out": e["H_TIMEDOUT"] == "1",
}, open(sys.argv[1], "w"))
HARVEST_SPEC_EOF
  )
}

# The git dir is created by ensure-repo under $GIT_ROOT, 0700, outside every mount. Do not mkdir it here.
mkdir -p "$REPO"
chmod 0750 "$REPO"
# An ensure-repo refusal ends the turn with a record. Never re-init over a directory that may hold history.
if ! _ensure_err="$(python3 "$HARVEST" ensure-repo \
        "$GIT_DIR_PATH" "$REPO" "$GIT_ROOT" 2>&1 >/dev/null)"; then
  _reason="$(printf '%s' "$_ensure_err" | tail -n1)"
  _reason="${_reason#apps-harvest: }"
  [ -n "$_reason" ] || _reason="ensure-repo failed and said nothing"
  _write_harvest_spec 1 0
  python3 "$HARVEST" refuse "$_spec" "$_reason" >/dev/null \
    || _say "could not even write the refusal record — the broker must synthesize"
  rm -f "$_spec"
  _die "$_reason"
fi

# scaffolded is derived host-side from the filesystem, never from runner output.
python3 "$HARVEST" watch "$REPO" "$RESULT_DIR/scaffolded" \
        "$STARTED_AT_EPOCH" "$((STARTED_AT_EPOCH + TURN_MAX_SEC + 60))" \
        >/dev/null 2>&1 &
_watcher_pid=$!

# ── 3. the container ──
: > "$STDOUT_LOG"; chmod 0600 "$STDOUT_LOG"
: > "$STDERR_LOG"; chmod 0600 "$STDERR_LOG"

args=(
  run --rm
  # --init reaps orphaned children; a long agentic turn hits fork EAGAIN without it.
  --init
  # --replace: a container left by a killed turn must not fail every later turn with podman 125.
  --replace
  --name "$CONTAINER_NAME"
  --hostname "apps-builder"
  # keep-id: the seat's uid 1000 maps to the account that owns /srv/apps.
  --userns "keep-id:uid=1000,gid=1000"
  # Network: the model API is reachable and nothing else, by host nftables keyed on this uid.
  --network "$NETWORK"
  # /work is the only persistent writable mount.
  -v "$REPO:/work:rw"
  # Nothing from the credential directory is mounted: an env.old left by rotation would publish the key.
  # Per-turn tmpfs HOME, with --mount because --tmpfs rejects uid= and gid=.
  --mount "type=tmpfs,destination=/home/resident,tmpfs-size=512m,tmpfs-mode=0700,U=true"
  --workdir /work
  # -i: podman drops stdin without it.
  -i
)

# ── credential block ──
# The value never goes in argv (`--env VAR` names it). The env file is filtered on fd 9, since podman cannot drop one var.
# /config/env is masked with /dev/null: this hides the file copy only.
# The key is dedicated, metered and rotatable: that is the containment (spec §D).
# This seat accepts only ANTHROPIC_API_KEY, so there is no Max refusal branch.
_cred_name=""
if [ -f "$ENV_FILE" ]; then
  _apikey="$(grep -E '^[[:space:]]*ANTHROPIC_API_KEY=' "$ENV_FILE" | tail -n1 || true)"
  _apikey="${_apikey#*=}"
  if [ -n "$_apikey" ]; then
    _cred_name="ANTHROPIC_API_KEY"
    _say "auth: ANTHROPIC_API_KEY from $ENV_FILE (dedicated metered key, spec §D)"
  else
    _say "WARNING no ANTHROPIC_API_KEY in $ENV_FILE — the turn will fail to authenticate"
  fi
  _filtered="$(mktemp "${TMPDIR:-/tmp}/${_tag}-env.XXXXXXXX")"
  chmod 0600 "$_filtered"
  grep -vE '^[[:space:]]*ANTHROPIC_API_KEY([[:space:]]*=|[[:space:]]*$)' \
    "$ENV_FILE" > "$_filtered" || true
  exec 9<"$_filtered"
  rm -f "$_filtered"
  args+=( --env-file /dev/fd/9 )
else
  _say "WARNING credential file absent: $ENV_FILE — the turn will fail to authenticate"
fi
unset ANTHROPIC_API_KEY
if [ -n "$_cred_name" ]; then
  export "$_cred_name=$_apikey"
  args+=( --env "$_cred_name" )
fi
unset _apikey
# ── END credential block ──

# The only house-related env inside the container: nothing in it can call home.
args+=(
  -e "PONYTAIL_DEFAULT_MODE=$PONYTAIL_MODE"
  -e "APPS_SESSION=$SESSION"
  -e "APPS_TURN=$TURN"
)

if [ -n "${APPS_PODMAN_EXTRA:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of keyboard-set flags
  args+=( ${APPS_PODMAN_EXTRA} )
fi

# The runner argv comes from the launcher's JSON. Python decodes it, since bash cannot parse it safely.
mapfile -t _runner < <(python3 -c '
import json, sys
for a in json.loads(sys.argv[1]):
    print(a)
' "$RUNNER_COMMAND")
[ "${#_runner[@]}" -gt 0 ] || _die "APPS_RUNNER_COMMAND is empty"

args+=( "$IMAGE" "${_runner[@]}" --model "$MODEL" )

_say "turn $SESSION/$TURN app=$APP_ID image=$IMAGE model=$MODEL unit-clock=${TURN_MAX_SEC}s"

# RuntimeMaxSec SIGTERMs the whole cgroup, this script included: trap it, kill the container, and still harvest.
_timed_out=0
_podman_rc=0
_on_term() {
  _timed_out=1
  _say "SIGTERM (the unit's RuntimeMaxSec, or a stop) — ending the turn and harvesting"
  kill -TERM "$_podman_pid" 2>/dev/null || true
  "$PODMAN" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}

set +e
"$PODMAN" "${args[@]}" < "$PROMPT" > "$STDOUT_LOG" 2> "$STDERR_LOG" &
_podman_pid=$!
trap _on_term TERM INT
# wait returns early on a trapped signal, so it is retried until the child is gone.
while :; do
  wait "$_podman_pid"; _podman_rc=$?
  kill -0 "$_podman_pid" 2>/dev/null || break
done
trap - TERM INT
set -e

kill -TERM "$_watcher_pid" 2>/dev/null || true
wait "$_watcher_pid" 2>/dev/null || true

if [ "$_timed_out" = 1 ] && [ "$_podman_rc" = 0 ]; then
  # A container killed mid-turn can still exit 0. The clock is the truth, so record 143.
  _podman_rc=143
fi
_say "runner exit $_podman_rc (timed_out=$_timed_out)"

# ── 4. the harvest ──
_write_harvest_spec "$_podman_rc" "$_timed_out"

if python3 "$HARVEST" harvest "$_spec"; then
  rm -f "$_spec"
  _say "harvest ok — $RESULT_DIR/result.json"
  # Exit 0 when the harvest succeeded, whatever the turn's status: the record says what happened.
  exit 0
fi

rm -f "$_spec"
_die "harvest FAILED for turn $SESSION/$TURN (runner exit $_podman_rc) — no result.json"
