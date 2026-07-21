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
#     <slug>           the spec slug (branch is loop/<slug>); used only to name
#                      the container. Broker-validated kebab, safe as an arg.
#     [command...]     the headless CC build-session argv, forwarded verbatim.
#                      Carries the WP-L5 model pin: the broker appends
#                      `--model <id>`, which rides into the container command via
#                      "$@" (identical mechanism to run-resident.sh).
#
# The SPEC (the chat-derived design) is fed to the session on STDIN, never in
# argv — the launcher.py doctrine: argv is config, chat is data.
#
# NO MERGE, NO PUSH, NO PROD: the build lands on its branch in the worktree and
# waits for a human. The egress wall (WP-H2 host nftables on the res-* uid)
# already blocks external git; this script adds no remote and grants no path to
# production. Human merges; nothing lands itself.
#
# Overridable env (defaults are the production layout — mirror run-resident.sh):
#   RESIDENT_IMAGE           image ref        (localhost/disjorn-resident:latest)
#   RESIDENT_HOME_VOL        host home volume ($HOME/resident-home) — the git
#                            worktree the build commits to; mounted RW.
#   RESIDENT_CONFIG_DIR      host config dir  (/home/plink/resident-config/<name>)
#   RESIDENT_BROKER_SOCKET   host socket path (/run/disjorn-broker/broker.sock)
#   RESIDENT_HOUSE_MEMORY    host package dir (…/harness/house_memory)
#   RESIDENT_NETWORK         podman network   (pasta; real egress wall is WP-H2)
#   RESIDENT_PODMAN_EXTRA    extra podman-run flags (word-split; e.g. "-d")
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
HOME_VOL="${RESIDENT_HOME_VOL:-$HOME/resident-home}"
CONFIG_DIR="${RESIDENT_CONFIG_DIR:-/home/plink/resident-config/$NAME}"
BROKER_SOCK="${RESIDENT_BROKER_SOCKET:-/run/disjorn-broker/broker.sock}"
HOUSE_MEMORY="${RESIDENT_HOUSE_MEMORY:-/home/plink/Disjorn/Disjorn/harness/house_memory}"
NETWORK="${RESIDENT_NETWORK:-pasta}"

[ -d "$HOME_VOL" ] || { echo "run-build: home volume missing: $HOME_VOL" >&2; exit 1; }
[ -d "$CONFIG_DIR" ] || { echo "run-build: config dir missing: $CONFIG_DIR" >&2; exit 1; }
[ -S "$BROKER_SOCK" ] || echo "run-build: WARNING broker socket absent: $BROKER_SOCK (broker calls will fail)" >&2

args=(
  run --rm
  # Per-build container name so concurrent builds never collide; the slug is
  # broker-validated kebab (branch/argv-safe).
  --name "disjorn-build-$SLUG"
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
  # Mount the socket's DIRECTORY, not the socket file (a bind-mounted socket
  # inode goes dead on broker restart — see run-resident.sh). BROKER_SOCKET
  # tells the build session's broker CLI where to look.
  -v "$(dirname "$BROKER_SOCK"):/run/disjorn-broker:ro"
  -e "BROKER_SOCKET=/run/disjorn-broker/$(basename "$BROKER_SOCK")"
  -v "$CONFIG_DIR:/config:ro"
  # The spec is fed on stdin; podman drops stdin without -i (always, unlike the
  # opt-in in run-resident.sh — a build with no spec is meaningless).
  -i
)

if [ -d "$HOUSE_MEMORY" ]; then
  args+=( -v "$HOUSE_MEMORY:/opt/house_memory:ro" )
else
  echo "run-build: WARNING house_memory absent: $HOUSE_MEMORY (skipping mount)" >&2
fi

ENV_FILE="$CONFIG_DIR/env"
if [ -f "$ENV_FILE" ]; then
  args+=( --env-file "$ENV_FILE" )
else
  echo "run-build: WARNING env file absent: $ENV_FILE (no ANTHROPIC_API_KEY)" >&2
fi

if [ -n "${RESIDENT_PODMAN_EXTRA:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of extra flags
  args+=( ${RESIDENT_PODMAN_EXTRA} )
fi

args+=( "$IMAGE" )
# Everything after <resident> <slug> is forwarded verbatim as the container
# command. This carries the build session_argv AND the WP-L5 model pin: the
# broker appends `--model <id>` to the argv, so it arrives here in "$@" and
# rides into the container command unchanged — identical to run-resident.sh.
[ "$#" -gt 0 ] && args+=( "$@" )

exec podman "${args[@]}"
