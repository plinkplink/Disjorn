#!/usr/bin/env bash
# run-gates.sh <resident> <slug> — the broker's test run for loop/<slug>.
#
# A green gate proves the run happened and was not skipped; it runs the
# branch's own suite, so it is not a review. Stdout carries only these lines;
# every other byte goes to stderr.
#
#   GATE tests pass|fail
#   GATE typecheck pass|fail|skipped
#   GATE build pass|fail|skipped
#   GATE exit <n>
#
# node_modules is mounted read-only from the deployed tree, so a branch that
# adds a dependency is typechecked and built without it and will read red
# until the keyboard installs it.
set -uo pipefail

TAG=run-gates

die() { echo "$TAG: $*" >&2; exit 2; }

[ "$#" -eq 2 ] || die "usage: run-gates.sh <resident> <slug>"
NAME="$1"
SLUG="$2"

# Re-validated here too: this script is reachable as a resident uid.
[[ "$NAME" =~ ^[a-z][a-z0-9]{0,30}$ ]] || die "resident is not a plain lowercase name: $NAME"
[ "${#SLUG}" -le 64 ] || die "slug too long"
[[ "$SLUG" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9][a-z0-9-]{0,50}$ ]] \
  || die "slug is not a dated kebab name: $SLUG"

IMAGE="${RESIDENT_IMAGE:-localhost/disjorn-resident:latest}"
GATEHOUSE="${RESIDENT_GATEHOUSE:-/var/lib/disjorn-broker/gatehouse}"
# Mounted, not installed: `npm install` needs a network this gate lacks.
NODE_MODULES="${RESIDENT_CLIENT_NODE_MODULES:-/srv/disjorn-client-node-modules}"
BARE="$GATEHOUSE/disjorn.git"
BRANCH="loop/$SLUG"
RUN_ROOT="${RESIDENT_GATE_RUNS:-$HOME/gate-runs}"

[ -d "$GATEHOUSE" ] || die "gatehouse missing: $GATEHOUSE"
[ -d "$BARE" ] || die "gatehouse repo missing: $BARE"

mkdir -p "$RUN_ROOT" || die "cannot create $RUN_ROOT"
# A checkout of its own per run, and the container is torn down before the
# tree it mounts: killing the podman client leaves the container up.
WORK="$(mktemp -d "$RUN_ROOT/$SLUG.XXXXXX")" || die "cannot create a run dir in $RUN_ROOT"
CONTAINER="disjorn-gate-$SLUG-$$"
cleanup() {
  podman rm -f -t 0 --ignore "$CONTAINER" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

git clone --quiet --single-branch --branch main "$BARE" "$WORK" >&2 \
  || die "cannot clone $BARE"
git -C "$WORK" fetch --quiet "$BARE" "refs/heads/$BRANCH" >&2 \
  || die "no such branch in the gatehouse: $BRANCH"
git -C "$WORK" checkout --quiet --detach FETCH_HEAD >&2 \
  || die "cannot check out $BRANCH"

CLIENT_CHANGED="$(git -C "$WORK" diff --name-only main...HEAD -- client/ 2>/dev/null | head -1)"

mounts=( -v "$WORK:/work" )
client_gate=1
if [ -n "$CLIENT_CHANGED" ]; then
  if [ -d "$NODE_MODULES" ]; then
    mounts+=( -v "$NODE_MODULES:/work/client/node_modules:ro" )
  else
    # Unreadable is not skipped: a gate that did not run is red.
    echo "$TAG: client/ changed but $NODE_MODULES is not a directory this uid ($(id -un)) can see — the client gates cannot run" >&2
    client_gate=0
  fi
fi

# PYTHONPATH names the in-tree house_memory package; the image has no copy.
INNER='
set -u
cd /work/server && python3 -m pytest tests -q -p no:cacheprovider >&2
_server=$?
export PYTHONPATH=/work/harness/house_memory
cd /work && python3 -m pytest harness -q -p no:cacheprovider --ignore=harness/cc/tests/test_container.sh >&2
_harness=$?
if [ "$_server" -eq 0 ] && [ "$_harness" -eq 0 ]; then
  echo "GATE tests pass"
else
  echo "GATE tests fail"
fi
if [ "${GATE_CLIENT:-0}" = "1" ]; then
  cd /work/client && npm run typecheck >&2
  _tc=$?
  if [ "$_tc" -eq 0 ]; then
    echo "GATE typecheck pass"
    npm run build >&2
    if [ "$?" -eq 0 ]; then echo "GATE build pass"; else echo "GATE build fail"; fi
  else
    echo "GATE typecheck fail"
    echo "GATE build fail"
  fi
fi
'

# No network, ever: a gate that had one could install its way to green.
out="$(podman run --rm --network none \
  --name "$CONTAINER" \
  --userns "keep-id:uid=1000,gid=1000" \
  "${mounts[@]}" \
  -e "GATE_CLIENT=$([ -n "$CLIENT_CHANGED" ] && [ "$client_gate" = 1 ] && echo 1 || echo 0)" \
  "$IMAGE" bash -c "$INNER")"
podman_rc=$?

# A gate that printed no line is a fail, never a skip.
tests=fail
if [ -n "$CLIENT_CHANGED" ]; then
  typecheck=fail
  build=fail
else
  typecheck=skipped
  build=skipped
fi
while IFS= read -r line; do
  case "$line" in
    "GATE tests pass") tests=pass ;;
    "GATE typecheck pass") typecheck=pass ;;
    "GATE typecheck fail") typecheck=fail ;;
    "GATE build pass") build=pass ;;
    "GATE build fail") build=fail ;;
  esac
done <<< "$out"

if [ "$podman_rc" -ne 0 ]; then
  echo "$TAG: container exited $podman_rc" >&2
fi

echo "GATE tests $tests"
echo "GATE typecheck $typecheck"
echo "GATE build $build"

rc=0
for g in "$tests" "$typecheck" "$build"; do
  [ "$g" = fail ] && rc=1
done
echo "GATE exit $rc"
exit "$rc"
