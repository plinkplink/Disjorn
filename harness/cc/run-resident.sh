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
#   RESIDENT_PODMAN_EXTRA    extra podman-run flags (word-split; e.g. "-d")
#
# Secrets: ANTHROPIC_API_KEY comes from the per-resident env file
#   /home/plink/resident-config/<name>/env        (plink-owned, mounted ro)
# passed via --env-file. That file is DOCUMENTED here, never created here —
# plink writes it at the keyboard. Expected contents:
#   ANTHROPIC_API_KEY=sk-ant-...
#   # optional kill switch — any non-empty value makes the broker CLI refuse:
#   # BROKER_DISABLE=1
# If the file is missing the container still starts (useful for smoke tests);
# CC sessions will simply fail to authenticate.
#
# Mount map (must match the Containerfile's documented plan):
#   $RESIDENT_HOME_VOL       -> /home/resident      rw
#   $RESIDENT_BROKER_SOCKET  -> /run/broker.sock    ro   (socket)
#   $RESIDENT_CONFIG_DIR     -> /config             ro   (kill-switch surface)
#   $RESIDENT_HOUSE_MEMORY   -> /opt/house_memory   ro
#
# NOTE for the keyboard install (WP-H1 follow-up): res-* users cannot read
# /home/plink — copy this script to a world-readable path, e.g.
# /usr/local/lib/disjorn/run-resident.sh, and point the user unit there.
set -euo pipefail

NAME="${1:?usage: run-resident.sh <resident-name> [command...]}"
shift

IMAGE="${RESIDENT_IMAGE:-localhost/disjorn-resident:latest}"
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
  --name "resident-cc-$NAME"
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

ENV_FILE="$CONFIG_DIR/env"
if [ -f "$ENV_FILE" ]; then
  args+=( --env-file "$ENV_FILE" )
else
  echo "run-resident: WARNING env file absent: $ENV_FILE (no ANTHROPIC_API_KEY)" >&2
fi

if [ -n "${RESIDENT_PODMAN_EXTRA:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word-splitting of extra flags
  args+=( ${RESIDENT_PODMAN_EXTRA} )
fi

args+=( "$IMAGE" )
[ "$#" -gt 0 ] && args+=( "$@" )

exec podman "${args[@]}"
