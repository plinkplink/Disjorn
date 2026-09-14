#!/usr/bin/env bash
# run-apps.sh — one apps-builder turn, from inside the transient unit.
#
# INSTALLED AT /usr/local/lib/disjorn/run-apps.sh, root:root 0755 (the launcher
# refuses to start anything else — see check_wrapper there). RUNS AS
# res-appsbuilding, never as root, never as plink: systemd-run set the
# credentials before this process existed.
#
#     run-apps.sh <session> <turn> <app-id>      (the prompt arrives on stdin)
#
# Everything else arrives as APPS_* environment from the launcher, which is the
# only reader of /etc/disjorn-apps/launch.toml. This script reads no config.
#
# WHAT IT DOES, in order (spec §C/§E):
#   1. make the result directory, the app WORK TREE and the app's host-only
#      GIT DIR; `git init` the split repo if this is its first turn (as
#      res-appsbuilding — the broker runs as plink and must never write under
#      /srv/apps, Gable #2295);
#   2. start the in-unit WATCHER, which writes the `scaffolded` marker into the
#      result directory on the first change under /work after now. The broker
#      READS that marker; it never holds a watch of its own;
#   3. `podman run` the seat: /work rw, the image's own /shelf ro, /config ro
#      nothing from the credential dir mounted, a tmpfs HOME, the prompt on stdin, the runner's
#      stream spooled to 0600 files;
#   4. HARVEST via apps_harvest.py — the exit status decides everything and the
#      order is spec §E's, which that module implements and its test suite
#      pins;
#   5. exit 0 if the HARVEST succeeded, whatever the turn's own exit was. What
#      the broker needs is the record, and the record says what happened.
#
# THE SEAT HOLDS NO HOUSE CREDENTIAL (spec §A). There is no broker socket, no
# gatehouse, no house memory, no spine, no platform clone in any mount below.
# Its only inbound is the prompt on stdin; its only outbound is /work.
#
# THE WALL (SPECS/2026-09-13-apps-gitdir-outside-the-mount.md, D7)
# ----------------------------------------------------------------
# THE TURN'S WRITABLE SURFACE IS THE WORK TREE AND NOTHING ELSE. /work is
# $REPO_ROOT/<app-id> and it is the only persistent rw mount below. The app's
# REPOSITORY lives at $GIT_ROOT/<app-id>.git, which is mounted into no
# container, ever — not ro, not under another name, not "just for this one
# thing". Git metadata is host-only BY LOCATION.
#
# Until 2026-09-13 the repository was at /work/.git, so the turn wrote the
# config that the host's own `git add -A` then read, and `core.fsmonitor` in
# it was host code execution as res-appsbuilding (Gable #2546/#2557).
# A SHAPE CHECK ON A PATH THE TURN CAN WRITE IS NOT A BOUNDARY AND IS NEVER
# WRITTEN DOWN AS ONE (the gate.py #2561 discipline). If a future flag here
# ever mounts anything under $GIT_ROOT, this file's own test suite fails —
# and that test is the statement, not this paragraph.
set -euo pipefail

_tag="$(basename "$0" .sh)"
_say() { echo "$_tag: $*" >&2; }
_die() { _say "FATAL: $*"; exit 1; }

SESSION="${1:?usage: run-apps.sh <session> <turn> <app-id>   (prompt on stdin)}"
TURN="${2:?usage: run-apps.sh <session> <turn> <app-id>   (prompt on stdin)}"
APP_ID="${3:?usage: run-apps.sh <session> <turn> <app-id>   (prompt on stdin)}"

# The launcher validated all four against spec §C's charsets before any
# privilege was used. Re-asserted here anyway, cheaply: this script is also
# runnable by hand at the keyboard, and a boundary that trusts its caller is
# decoration.
[[ "$SESSION" =~ ^[1-9][0-9]{0,8}$ ]] || _die "session id: $SESSION"
[[ "$TURN"    =~ ^[1-9][0-9]{0,3}$ ]] || _die "turn: $TURN"
[[ "$APP_ID"  =~ ^[a-z2-7]{12}$    ]] || _die "app id: $APP_ID"
[ -t 0 ] && _die "the prompt arrives on stdin (the launcher feeds it); refusing a terminal"

IMAGE="${APPS_IMAGE:-localhost/disjorn-apps-builder:latest}"
MODEL="${APPS_MODEL:-claude-opus-5}"
PONYTAIL_MODE="${APPS_PONYTAIL_MODE:-full}"
RUNNER_COMMAND="${APPS_RUNNER_COMMAND:-[\"claude\",\"-p\",\"--output-format\",\"stream-json\",\"--verbose\"]}"
TURN_MAX_SEC="${APPS_TURN_MAX_SEC:-1800}"
REPO_ROOT="${APPS_REPO_ROOT:-/srv/apps}"
# The host-only git root (D1). NEVER MOUNTED, by this script or any other.
GIT_ROOT="${APPS_GIT_ROOT:-/srv/apps-git}"
TURNS_ROOT="${APPS_TURNS_ROOT:-/srv/apps-turns}"
WWW_ROOT="${APPS_WWW_ROOT:-/srv/apps-www}"
QUARANTINE_ROOT="${APPS_QUARANTINE_ROOT:-/srv/apps-quarantine}"
CONFIG_DIR="${APPS_CONFIG_DIR:-/srv/disjorn-build-config/appsbuilding}"
HARVEST="${APPS_HARVEST:-/usr/local/lib/disjorn/apps_harvest.py}"
NETWORK="${APPS_NETWORK:-pasta}"
PODMAN="${APPS_PODMAN:-podman}"

# The two trees, always named together and neither derived from the other.
REPO="$REPO_ROOT/$APP_ID"                 # the WORK TREE; this is /work
GIT_DIR_PATH="$GIT_ROOT/$APP_ID.git"      # the REPOSITORY; mounted nowhere
RESULT_DIR="$TURNS_ROOT/$SESSION/$TURN"
PREVIEW_DIR="$WWW_ROOT/$APP_ID/preview"
QUARANTINE_DIR="$QUARANTINE_ROOT/$APP_ID/$TURN"
ENV_FILE="$CONFIG_DIR/env"
CONTAINER_NAME="disjorn-apps-$SESSION-$TURN"

[ -x "$HARVEST" ] || [ -f "$HARVEST" ] || _die "harvest module missing: $HARVEST"

# 0755 dirs / 0644 files under the result root (spec §C): the broker reads and
# inotifies that directory without owning it. The spools are re-chmod'd 0600
# below — they are the runner's raw stream and ONLY THIS SEAT ever opens them:
# the harvest parses usage into result.json and redacts the key out of them
# in place (Claudette, slice (i) review). The broker, which runs as plink,
# reads result.json and nothing else (BL-D2).
umask 0022
# The RESULT dir only. The preview root is created by the harvest's publisher
# on the first publish, never here: a turn that never publishes (a secret
# halt on turn 1) must not leave an empty served root behind — "a preview
# exists" and "a preview worked" are one observable (Claudette #2336).
# A result.json already here is some EARLIER turn's — a re-used session number
# on a fresh database, a dir nobody cleared — and the broker's reaper reads
# "result.json exists" as "this turn finished". It did, once (2026-09-09,
# the first build after the flip finished in 30 ms with the 09-07 proving
# turn's record). Clear it before anything runs; the harvest writes ours.
# BEFORE the mkdir, not after (Claudette #2457): hygiene only has work to do
# when the dir pre-existed, and an rm that runs after the mkdir opens a
# window in which a stop marker the launcher just legitimately dropped —
# the early-stop case — is eaten, and the room says "timed out" on a turn
# the user stopped. On a fresh dir this is a silent no-op.
rm -f "$RESULT_DIR/result.json" "$RESULT_DIR/scaffolded" "$RESULT_DIR/stop-requested"
mkdir -p "$RESULT_DIR"
# The prompt: bounded bytes the launcher (root) read behind its path wall and
# put on OUR stdin. We copy it into our own result directory, as the seat,
# 0600 — root never writes here (Claudette's block, slice (i) review).
PROMPT="$RESULT_DIR/prompt.md"
# ONE bound, ONE number: the launcher enforced [apps].prompt_max_bytes off the
# fd before anything ran and passes the same number here; this head -c is a
# hand-run backstop with the same value, never a second policy.
PROMPT_MAX_BYTES="${APPS_PROMPT_MAX_BYTES:-65536}"
( umask 077 && head -c "$PROMPT_MAX_BYTES" > "$PROMPT" ) || _die "cannot stage the prompt"
[ -s "$PROMPT" ] || _die "empty prompt on stdin"
STARTED_AT_EPOCH="$(date +%s)"
STARTED_AT="$(date -u -Is)"
# Named here rather than at step 3: the harvest's argument file carries both
# paths and is written on the REFUSAL path too, before there is anything to
# spool. Truncating and chmod'ing them is still step 3's job.
STDOUT_LOG="$RESULT_DIR/stdout.log"
STDERR_LOG="$RESULT_DIR/stderr.log"

# ── the harvest's argument file ──────────────────────────────────────────────
# Fifteen arguments in one JSON file rather than fifteen positional slots: a
# positional call with fifteen slots is a defect waiting for its first reorder.
# The file lives in the result directory (0600 — it names the credential FILE,
# though never its value) and is removed on the way out.
#
# ONE WRITER, TWO READERS: `harvest` (the turn ran and ended) and `refuse` (the
# turn never started, because ensure-repo found a half-made git dir). The fields
# are the same fields, so they are written in one place; two argument builders
# for one record is how the two records drift.
#
#     _write_harvest_spec <exit-code> <timed-out>
#
# Every value crosses into python as ENVIRONMENT, never interpolated into the
# heredoc's source: a path with a quote in it would otherwise be a code
# injection into this script's own helper, and "the charsets make that
# impossible" is an argument, not a mechanism.
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

# ── 1. the app's two trees ───────────────────────────────────────────────────
# 0750 on the work tree: res-appsbuilding's own, and nothing else on the box
# reads an app's source. The PREVIEW root is the world-readable copy, and it is
# a copy for exactly this reason.
#
# The GIT DIR is created by ensure-repo, under $GIT_ROOT, 0700 — outside every
# mount below. This script does NOT mkdir it by name first: "already a repo" is
# ensure-repo's question and it answers it on $GIT_DIR_PATH/HEAD, which is a
# fact about the host-only tree.
mkdir -p "$REPO"
chmod 0750 "$REPO"
# A REFUSAL HERE ENDS THE TURN WITH A RECORD, not with silence. ensure-repo
# exits 2 when $GIT_DIR_PATH exists and is not a repository this house made —
# an interrupted init or an interrupted migration, which is an incident for the
# keyboard and never a reason to re-init over a directory that may hold the
# app's history. No container starts, nothing under either tree is written or
# deleted, and the broker reads the reason out of result.json instead of
# synthesizing "the unit died" (§E).
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

# ── 2. the in-unit watcher ───────────────────────────────────────────────────
# `scaffolded` is derived HOST-SIDE from the filesystem, never from runner
# output (spec §A), and never from a watch the broker has to be alive to hold.
# The deadline is the turn clock plus a minute so the watcher cannot outlive
# the unit even if the wait below is interrupted oddly.
python3 "$HARVEST" watch "$REPO" "$RESULT_DIR/scaffolded" \
        "$STARTED_AT_EPOCH" "$((STARTED_AT_EPOCH + TURN_MAX_SEC + 60))" \
        >/dev/null 2>&1 &
_watcher_pid=$!

# ── 3. the container ─────────────────────────────────────────────────────────
: > "$STDOUT_LOG"; chmod 0600 "$STDOUT_LOG"
: > "$STDERR_LOG"; chmod 0600 "$STDERR_LOG"

args=(
  run --rm
  # --init: catatonit as PID 1 reaps orphaned children. The build seat learned
  # this the hard way (git zombies hitting pids.max); a long agentic turn is a
  # build by another name.
  --init
  # --replace: a container left behind by a killed turn must not make every
  # subsequent turn of this session fail with podman 125.
  --replace
  --name "$CONTAINER_NAME"
  --hostname "apps-builder"
  # keep-id: res-appsbuilding appears INSIDE as uid 1000 ('resident'), so every
  # file the turn writes into /work is owned on the host by the one account
  # that owns /srv/apps. Identity is the venue, not a credential in a file.
  --userns "keep-id:uid=1000,gid=1000"
  # pasta plus the WP-H2 nftables wall, keyed on this uid: the model API is
  # reachable and nothing else is. pasta is plumbing; the wall is host policy
  # and already exists.
  --network "$NETWORK"
  # /work — THE ONLY WRITABLE PATH THAT PERSISTS. It may already contain an
  # app; the builder brief's first rule is to read it before writing.
  -v "$REPO:/work:rw"
  # NOTHING from the credential directory is mounted (Claudette, slice (i)
  # review): the first rotation that leaves an env.old beside env would
  # publish the key through a read-only /config mount. The brief, settings
  # and shelf are image content; the credential travels by NAME only.
  # A per-turn HOME on tmpfs: Claude Code needs a writable home for its own
  # state (Gable #2286), and it is discarded at exit, so nothing about one turn
  # leaks into the next except through the repo.
  # --mount, not --tmpfs: podman's --tmpfs takes no uid=/gid= (exit 125,
  # "unknown mount option"); U=true chowns the tmpfs to the container user.
  --mount "type=tmpfs,destination=/home/resident,tmpfs-size=512m,tmpfs-mode=0700,U=true"
  --workdir /work
  # The prompt arrives on stdin. podman drops stdin without -i.
  -i
)

# ── credential block (the run-resident.sh mechanism, verbatim in shape) ──────
# The VALUE never appears in argv: `--env VAR=value` would put the seat's API
# key into the process table, readable by any process on the host via
# /proc/*/cmdline. We use the NAME-ONLY form, so argv carries the name only.
# Everything else in the env file goes through podman's own --env-file parser
# via a FILTERED copy (podman offers no way to drop a var an env-file sets):
# created 0600, opened on fd 9, UNLINKED before exec, so it holds no credential
# and does not outlive the launch. podman passes no extra fds to the container.
# The env FILE is then masked with /dev/null inside, so the session cannot read
# the credential back out of /config/env — which removes the FILE copy only,
# and cannot hide the key from a session that necessarily has it in its own
# environment. The real containment is that this key is DEDICATED, METERED and
# ROTATABLE (spec §D); the harvest's secret scan is defence in depth.
#
# RESIDENT_METERED_OK semantics carry over as a statement of fact: this seat is
# metered deliberately, so there is no Max-account refusal branch here — an
# ANTHROPIC_API_KEY is the ONLY credential this seat accepts.
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
# ── END credential block ─────────────────────────────────────────────────────

# The only house-related environment inside the container (spec §A). No bot id,
# no session token, no socket path — there is nothing in here to call home with.
args+=(
  -e "PONYTAIL_DEFAULT_MODE=$PONYTAIL_MODE"
  -e "APPS_SESSION=$SESSION"
  -e "APPS_TURN=$TURN"
)

if [ -n "${APPS_PODMAN_EXTRA:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of keyboard-set flags
  args+=( ${APPS_PODMAN_EXTRA} )
fi

# The runner argv, from the launcher's JSON, with --model appended (spec §A).
# python3 does the JSON decode because bash has no parser and a hand-rolled one
# is exactly the kind of "clever" that eats a turn.
mapfile -t _runner < <(python3 -c '
import json, sys
for a in json.loads(sys.argv[1]):
    print(a)
' "$RUNNER_COMMAND")
[ "${#_runner[@]}" -gt 0 ] || _die "APPS_RUNNER_COMMAND is empty"

args+=( "$IMAGE" "${_runner[@]}" --model "$MODEL" )

_say "turn $SESSION/$TURN app=$APP_ID image=$IMAGE model=$MODEL unit-clock=${TURN_MAX_SEC}s"

# RuntimeMaxSec fires as a SIGTERM to the WHOLE cgroup — this script included.
# Without the trap the harvest would simply never run and the broker would wait
# forever on a result.json that no one was left to write. So: catch it, kill
# the container, record that the clock (not the runner) ended the turn, and
# fall through to the harvest. systemd's default TimeoutStopSec (90s) is the
# budget for everything below, which is ample for a commit and an rsync.
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
# `wait` returns early with 128+signum when a trapped signal arrives, so it is
# retried until the child is genuinely gone. Its LAST status is the turn's.
while :; do
  wait "$_podman_pid"; _podman_rc=$?
  kill -0 "$_podman_pid" 2>/dev/null || break
done
trap - TERM INT
set -e

kill -TERM "$_watcher_pid" 2>/dev/null || true
wait "$_watcher_pid" 2>/dev/null || true

if [ "$_timed_out" = 1 ] && [ "$_podman_rc" = 0 ]; then
  # A container killed mid-turn can still exit 0 through podman. The clock is
  # the truth here, so say so rather than publishing a half-built app.
  _podman_rc=143
fi
_say "runner exit $_podman_rc (timed_out=$_timed_out)"

# ── 4. the harvest ───────────────────────────────────────────────────────────
_write_harvest_spec "$_podman_rc" "$_timed_out"

if python3 "$HARVEST" harvest "$_spec"; then
  rm -f "$_spec"
  _say "harvest ok — $RESULT_DIR/result.json"
  # ── 5. exit 0 if the HARVEST succeeded, regardless of the turn's exit. The
  # turn's own status is IN the result; making it this process's status too
  # would tell the broker the same thing twice and tell systemd a halted turn
  # was a failed unit.
  exit 0
fi

rm -f "$_spec"
_die "harvest FAILED for turn $SESSION/$TURN (runner exit $_podman_rc) — no result.json"
