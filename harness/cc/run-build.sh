#!/usr/bin/env bash
# run-build.sh: podman run wrapper for one detached build session, run as the res-* user.
# Usage: run-build.sh <resident-name> <slug> [command...]. The spec arrives on stdin, never argv.
# The broker enforces the confirm gate, budget and wall-clock cap. The slug keeps its date prefix to match the branch.
# No merge, no prod: no writable path out of the container remains; the product leaves by the post-exit harvest.
# Res-* users cannot read /home/plink: the broker must point at a world-readable copy.
set -euo pipefail

NAME="${1:?usage: run-build.sh <resident-name> <slug> [command...]}"
shift
SLUG="${1:?usage: run-build.sh <resident-name> <slug> [command...]}"
shift

IMAGE="${RESIDENT_IMAGE:-localhost/disjorn-resident:latest}"
# --name and the reaper share this name.
CONTAINER_NAME="disjorn-build-$SLUG"
# The build seat has its own home, config and kernel: a build inside the resident's home wears her settings.
HOME_VOL="${RESIDENT_HOME_VOL:-$HOME/build-home}"
CONFIG_DIR="${RESIDENT_CONFIG_DIR:-/home/plink/build-config/$NAME}"
HOUSE_MEMORY="${RESIDENT_HOUSE_MEMORY:-/home/plink/Disjorn/Disjorn/harness/house_memory}"
# pasta is plumbing, not the wall: egress is the host nftables rule on the res-* uid.
NETWORK="${RESIDENT_NETWORK:-pasta}"
# Copied to ~/.claude/CLAUDE.md: no bootstrap runs before claude.
# bootstrap.py would overwrite this copy once the spine is wired in (BUILD-SEAT-CONTRACT.md § Kernel).
BUILD_KERNEL="${RESIDENT_BUILD_KERNEL:-/usr/local/lib/disjorn/build-kernel.md}"
# Bare, so no working tree exists and a push cannot deploy.
GATEHOUSE="${RESIDENT_GATEHOUSE:-/var/lib/disjorn-broker/gatehouse}"

[ -d "$HOME_VOL" ] || { echo "run-build: build home missing: $HOME_VOL" >&2; exit 1; }
# Walk the parents: a uid locked out of a parent sees an existing directory as missing.
_p="$CONFIG_DIR"
while [ "$_p" != "/" ]; do
  _p="$(dirname "$_p")"
  [ -e "$_p" ] || continue
  [ -x "$_p" ] || { echo "run-build: build config dir UNREACHABLE: $(id -un) cannot traverse $_p, the innermost ancestor it cannot enter (mode $(stat -c %a "$_p" 2>/dev/null || echo ?)) — $CONFIG_DIR may well exist" >&2; exit 1; }
done
[ -d "$CONFIG_DIR" ] || { echo "run-build: build config dir missing: $CONFIG_DIR" >&2; exit 1; }
# A present env this uid cannot read is refused by name; an absent one only warns.
if [ -e "$CONFIG_DIR/env" ] && [ ! -r "$CONFIG_DIR/env" ]; then
  echo "run-build: build credential exists but is UNREADABLE by $(id -un): $CONFIG_DIR/env ($(stat -c '%a %U:%G' "$CONFIG_DIR/env" 2>/dev/null))" >&2; exit 1
fi
[ -f "$BUILD_KERNEL" ] || { echo "run-build: build kernel missing: $BUILD_KERNEL" >&2; exit 1; }
[ -d "$GATEHOUSE" ] || { echo "run-build: gatehouse missing: $GATEHOUSE" >&2; exit 1; }

# ── BEGIN provisioning ──
# Provisioning runs on the host as res-<name>, so a failure is loud before the model is invoked.
BRANCH="loop/$SLUG"
WORK="$HOME_VOL/work"

# Copied, not mounted: a bind mount there would fight the home volume.
mkdir -p "$HOME_VOL/.claude" "$WORK"
cp -f "$BUILD_KERNEL" "$HOME_VOL/.claude/CLAUDE.md"

# Fresh clone every run: a stale checkout silently builds against yesterday's tree.
# Clone only the entitled set (disjorn and this seat's repo). A missing entitled repo refuses before podman runs.
ENTITLED=( disjorn )
[ "$NAME" = "disjorn" ] || ENTITLED+=( "$NAME" )
# Clone-point SHAs stay in memory: the home volume is writable by the measured session.
BASE_SHA=()

for _repo in "${ENTITLED[@]}"; do
  _repo_path="$GATEHOUSE/$_repo.git"
  _dest="$WORK/$_repo"
  [ -d "$_repo_path" ] || {
    echo "run-build: REFUSING TO LAUNCH: entitled repo missing from the gatehouse: $_repo_path" >&2
    echo "run-build: the entitled set for resident '$NAME' is: ${ENTITLED[*]}. Create it at the keyboard with 'harness/keyboard/08-gatehouse-repo.sh create $_repo $NAME' (the recipe applies ownership, setgid and safe.directory together, from the first byte)." >&2
    exit 1; }

  # Quarantine: a clone holding loop/* commits that no gatehouse ref reaches is moved aside, never deleted.
  # Reachability is checked against gatehouse refs: a push can land every object and die at the ref update, and object existence decays under gc.
  if [ -d "$_dest/.git" ]; then
    _q_branches=""
    while IFS= read -r _wbranch; do
      [ -n "$_wbranch" ] || continue
      _whead="$(git -C "$_dest" rev-parse --verify -q "refs/heads/$_wbranch" 2>/dev/null)" || continue
      [ -n "$_whead" ] || continue
      if git -C "$_repo_path" cat-file -e "$_whead^{commit}" 2>/dev/null \
         && [ -n "$(git -C "$_repo_path" for-each-ref --contains "$_whead" --count=1 2>/dev/null)" ]; then
        continue   # a gatehouse ref reaches it: harvested (or clone-point), safe to delete
      fi
      _q_branches="${_q_branches:+$_q_branches }$_wbranch"
    done < <(git -C "$_dest" for-each-ref --format='%(refname:short)' 'refs/heads/loop/*' 2>/dev/null)

    if [ -n "$_q_branches" ]; then
      _q_slug="${_q_branches%% *}"; _q_slug="${_q_slug#loop/}"
      mkdir -p "$HOME_VOL/quarantine"
      _q_path="$HOME_VOL/quarantine/$_repo-$_q_slug-$(date +%s)"
      mv "$_dest" "$_q_path" || {
        echo "run-build: REFUSING TO LAUNCH: could not quarantine $_dest -> $_q_path; it holds unharvested commits on: $_q_branches" >&2
        exit 1; }
      echo "QUARANTINED $_repo $_q_path"
      echo "run-build: $_dest held unharvested commits on: $_q_branches — moved to $_q_path rather than deleted. Nothing else will touch it; recover or discard it at the keyboard." >&2
    fi
    unset _q_branches _q_slug _q_path _wbranch _whead
  fi

  rm -rf "$_dest"
  # --single-branch: the build sees main and its own loop/<slug> only.
  git clone --quiet --single-branch "$_repo_path" "$_dest" || {
    echo "run-build: FAILED to clone $_repo_path -> $_dest" >&2; exit 1; }
  git -C "$_dest" checkout --quiet -b "$BRANCH" || {
    echo "run-build: FAILED to create $BRANCH in $_dest" >&2; exit 1; }
  # Local identity: the author is the build seat, not the resident.
  git -C "$_dest" config user.name  "disjorn-build"
  git -C "$_dest" config user.email "build@disjorn.local"
  # Origin is removed, not repointed: a remote path that looks real sends the session debugging transport errors.
  git -C "$_dest" remote remove origin
  # An empty base means every commit on the branch is new.
  BASE_SHA+=( "$(git -C "$_dest" rev-parse --verify -q HEAD || true)" )
done
unset _repo_path _repo _dest
# ── END provisioning ──


args=(
  run --rm
  # --init reaps orphaned children; without it long builds hit fork EAGAIN.
  --init
  # Per-build name, so concurrent builds never collide.
  --name "$CONTAINER_NAME"
  --hostname "build-$SLUG"
  # keep-id: SO_PEERCRED at the broker carries the res-* uid.
  --userns "keep-id:uid=1000,gid=1000"
  --network "$NETWORK"
  # The worktree is read-write: the build commits to loop/<slug> here.
  -v "$HOME_VOL:/home/resident"
  # NO BROKER SOCKET: a build that can start builds cannot be told apart from the resident in the audit.
  -v "$CONFIG_DIR:/config:ro"
  # No gatehouse mount. Do not add it back to make pushing work: pushing worked only by a uid accident.
  # -i is required: the spec arrives on stdin.
  -i
)

# NO /opt/house_memory: house edits go in the worktree and are installed by the keyboard after merge.
: "${HOUSE_MEMORY:=}"  # retained only so the launcher's --setenv stays harmless

# /opt/disjorn is the path_map prefix classify-diff needs: a git-clean res-readable clone, never the live tree.
if [ -n "${RESIDENT_DISJORN_RO:-}" ]; then
  [ -d "$RESIDENT_DISJORN_RO" ] || { echo "run-build: RESIDENT_DISJORN_RO not a dir: $RESIDENT_DISJORN_RO" >&2; exit 1; }
  args+=( -v "$RESIDENT_DISJORN_RO:/opt/disjorn:ro" )
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

# Build routing refuses: Max only, so a build holding only an API key does not launch.
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

# RESIDENT_SEAT=build, after the env file. Must differ from run-resident.sh.
args+=( -e "RESIDENT_SEAT=build" )

if [ -n "${RESIDENT_PODMAN_EXTRA:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of extra flags
  args+=( ${RESIDENT_PODMAN_EXTRA} )
fi

# ── BEGIN dependency preflight ──
# Refuse before spawn if the seat cannot import its test stack: an unrunnable seat burns a slot.
# A canary, one import per stack; test_image_deps.py checks the repo, this checks the image.
PREFLIGHT_IMPORTS="${RESIDENT_PREFLIGHT_IMPORTS:-pytest aiosqlite fastapi httpx argon2 chromadb voyageai}"
# 78 (EX_CONFIG) refunds the build slot; any other nonzero exit spends it.
PREFLIGHT_EXIT=78
if [ "${RESIDENT_PREFLIGHT:-1}" = "1" ]; then
  # Import importlib.util: the bare form raises AttributeError, and a crashed probe must not read as success.
  _pf_py='import importlib.util, sys
missing=[m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]
print(" ".join(missing))
sys.exit(1 if missing else 0)'
  # Probe in the real image, uid and userns.
  # Use `if`: under set -e a failing command substitution exits before the status is read.
  if _pf_missing="$(podman run --rm --userns "keep-id:uid=1000,gid=1000" \
      "$IMAGE" python3 -c "$_pf_py" $PREFLIGHT_IMPORTS 2>/dev/null)"; then
    _pf_rc=0
  else
    _pf_rc=$?
  fi
  # 0 launches; 1 names the gaps; anything else also refuses. An unrunnable probe is no green light.
  if [ "$_pf_rc" -eq 1 ]; then
    echo "PREFLIGHT-FAILED: the build image cannot import:${_pf_missing}" >&2
    echo "run-build: refusing to start — a session in this seat would write tests it cannot run." >&2
    echo "run-build: image=$IMAGE. Fix: rebuild it with harness/keyboard/07-resident-image.sh, then retry." >&2
    echo "run-build: no build slot has been spent." >&2
    exit "$PREFLIGHT_EXIT"
  elif [ "$_pf_rc" -ne 0 ]; then
    echo "PREFLIGHT-FAILED: the dependency probe could not run in $IMAGE (exit $_pf_rc)." >&2
    echo "run-build: refusing rather than assuming the seat is fit — an uninterrogable seat is not a passing one." >&2
    echo "run-build: no build slot has been spent." >&2
    exit "$PREFLIGHT_EXIT"
  fi
  unset _pf_py _pf_missing _pf_rc
fi
# ── END dependency preflight ──

# ── BEGIN container reaper block ──
# Not byte-identical with run-resident.sh: this wrapper outlives its container, so it also needs the trap.
# The watchdog covers SIGKILL, which runs no trap. Reap by --cidfile ID, never by name.
# RESIDENT_REAP=0 disables both mechanisms for debugging.
_reap_tag="$(basename "$0" .sh)"
# Computed once: the reaper and the launch tail must agree on whether this run is detached.
DETACHED=0
for _w in ${RESIDENT_PODMAN_EXTRA:-}; do
  case "$_w" in -d|--detach|--detach=true) DETACHED=1 ;; esac
done
_reap_cid=""
if [ "${RESIDENT_REAP:-1}" = "0" ]; then
  echo "$_reap_tag: WARNING RESIDENT_REAP=0 — container $CONTAINER_NAME will OUTLIVE this wrapper if the wrapper is killed; a refused or timed-out session keeps running inside it, writing into $WORK while the next launch deletes it" >&2
elif [ "$DETACHED" = "1" ]; then
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

  # The trap reaps the same way on signals it can see; its output is discarded so no reap message reaches the broker.
  _build_reap() {
    if [ -n "${_podman_pid:-}" ]; then
      kill "$_podman_pid" 2>/dev/null || true
    fi
    if [ -n "$_reap_cid" ] && [ -s "$_reap_cid" ]; then
      podman rm -f -t 0 --ignore "$(cat "$_reap_cid")" >/dev/null 2>&1 || true
    fi
    return 0
  }
  # INT and TERM exit 128+signal, so the broker sees a kill rather than a clean run.
  trap '_build_reap' EXIT
  trap '_build_reap; exit 130' INT
  trap '_build_reap; exit 143' TERM
fi
unset _reap_tag _w
# ── END container reaper block ──

args+=( "$IMAGE" )
# Forwarded verbatim, including the broker's model pin.
[ "$#" -gt 0 ] && args+=( "$@" )


# ── BEGIN launch + post-exit harvest ──
# Detached keeps the plain exec: nothing has run to harvest.
if [ "$DETACHED" = "1" ]; then
  exec podman "${args[@]}"
fi

# Foreground, not exec: this wrapper must outlive the container to publish. & and wait let the trap fire.
# <&0 stays: bash points a background command's stdin at /dev/null, and stdin carries the spec.
podman "${args[@]}" <&0 &
_podman_pid=$!
_container_rc=0
wait "$_podman_pid" || _container_rc=$?
_podman_pid=""

# Harvest only after a clean exit. The clones stay in place for the next launch's quarantine.
if [ "$_container_rc" -ne 0 ]; then
  echo "HARVEST-SKIPPED container exited $_container_rc"
  echo "run-build: container exited $_container_rc — not publishing. The workspace clones under $WORK are left in place; the next launch will quarantine any that hold unharvested commits." >&2
  exit "$_container_rc"
fi

# THE HARVEST: one stdout line per entitled repo, the only source of the broker's done-banner.
# PUBLISHED sha, NO-COMMITS, or PUBLISH-FAILED. Silence means failure. No force, ever.
_publish_failed=0
for _i in "${!ENTITLED[@]}"; do
  _repo="${ENTITLED[$_i]}"
  _base="${BASE_SHA[$_i]}"
  _dest="$WORK/$_repo"
  _bare="$GATEHOUSE/$_repo.git"

  _head="$(git -C "$_dest" rev-parse --verify -q "refs/heads/$BRANCH" 2>/dev/null || true)"
  if [ -z "$_head" ]; then
    echo "NO-COMMITS $_repo.git"
    continue
  fi
  # Count commits beyond the clone point, so a rebased session still gets an honest answer.
  if [ -n "$_base" ]; then
    _new="$(git -C "$_dest" rev-list --count "$_base..$BRANCH" 2>/dev/null || echo 0)"
  else
    _new="$(git -C "$_dest" rev-list --count "$BRANCH" 2>/dev/null || echo 0)"
  fi
  if [ "$_new" -eq 0 ]; then
    echo "NO-COMMITS $_repo.git"
    continue
  fi

  # Push the measured SHA, not HEAD, so the printed line and the pushed object cannot drift.
  _err="$(git -C "$_dest" push "$_bare" "$_head:refs/heads/$BRANCH" 2>&1)" || {
    echo "PUBLISH-FAILED $_repo.git $(printf '%s' "$_err" | tr '\n\r\t' '   ' | tr -s ' ')"
    _publish_failed=1
    continue
  }
  # Measure in the gatehouse, not the clone: a push exiting 0 is a transport claim only.
  _landed="$(git -C "$_bare" rev-parse --verify -q "refs/heads/$BRANCH" 2>/dev/null || true)"
  if [ "$_landed" = "$_head" ]; then
    echo "PUBLISHED $_repo.git $_head"
  else
    echo "PUBLISH-FAILED $_repo.git push reported success but $_bare refs/heads/$BRANCH is '${_landed:-absent}', not $_head"
    _publish_failed=1
  fi
done
unset _i _repo _base _dest _bare _head _new _err _landed

# Exit 0 only if the container was clean and every repo reported PUBLISHED or NO-COMMITS.
if [ "$_publish_failed" -ne 0 ]; then
  exit 1
fi
exit 0
# ── END launch + post-exit harvest ──
