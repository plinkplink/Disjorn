#!/usr/bin/env bash
# 07-resident-image.sh — rebuild the resident runtime image and hand it to
# every resident's per-user podman store (KEYBOARD-NEXT step 2, repeatable).
#
# RUN BY: plink, at his keyboard, as himself (NOT sudo bash — the build must
# land in plink's store; the script sudos per-resident where needed):
#
#     bash harness/keyboard/07-resident-image.sh
#
# Idempotent: rebuilding an unchanged Containerfile is a cache hit; reloading
# an image the resident already has is a no-op.
#
# WHEN to run it: only when the RUNTIME changes — the broker CLI grows or
# changes a verb, the Claude Code version bumps, a system dep is added to
# harness/cc/Containerfile. NOT for Disjorn code changes (that's the mirror,
# refreshed by the residents' own `refresh-mirror` verb) and NOT for resident
# personal code (that lives in their home volumes, untouched by this).
#
# WHY the dance (podman stores are PER-USER): plink's store, res-gable's and
# res-claudette's are three separate databases. Building in plink's store does
# nothing for the residents, and residents cannot build their own — the WP-H2
# egress wall blocks registry pulls from their uids, deliberately. So: build
# where there's egress, save to a tarball, load into each resident's store.
# Image updates are a plink action, permanently. This script IS that action.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE=localhost/disjorn-resident:latest
TAR=/var/tmp/disjorn-resident.tar   # /var/tmp, NOT /tmp: /tmp is tmpfs here
                                    # and the archive is ~1 GB of RAM wasted
RESIDENTS=(res-gable res-claudette)
# Verbs the freshly built CLI must answer to; extend when the broker grows one.
EXPECT_VERBS=(refresh-mirror start-build)

if [[ $EUID -eq 0 ]]; then
  echo "run as plink, not root — the build must land in plink's own store" >&2
  exit 1
fi

echo "== build (plink's store, has registry egress) =="
podman build -t "$IMAGE" -f "$REPO/harness/cc/Containerfile" "$REPO/harness/cc"
NEW_ID=$(podman inspect --format '{{.Id}}' "$IMAGE")
echo "built ${NEW_ID:0:12}"

echo "== save =="
podman save -o "$TAR" "$IMAGE"
chmod 0644 "$TAR"
trap 'rm -f "$TAR"' EXIT

for u in "${RESIDENTS[@]}"; do
  uid=$(id -u "$u")
  # subshell cd /: sudo -u from inside /home/plink dies on the 0700 home
  run_as() { (cd / && sudo -u "$u" env XDG_RUNTIME_DIR="/run/user/$uid" HOME="/home/$u" "$@"); }

  echo "== $u: load =="
  run_as podman load -i "$TAR"

  echo "== $u: verify the CLI answers to: ${EXPECT_VERBS[*]} =="
  help_out=$(run_as podman run --rm --network none "$IMAGE" broker --help)
  for verb in "${EXPECT_VERBS[@]}"; do
    grep -q "$verb" <<<"$help_out" || { echo "FAIL: $u image lacks '$verb'" >&2; exit 1; }
  done
  echo "ok"

  # A long-running container (Claudette's disjorn_bot) keeps its OLD image
  # until restarted; Gable's summons spawn fresh from :latest automatically.
  stale=$(run_as podman ps --format '{{.Names}} {{.ImageID}}' \
          | awk -v id="${NEW_ID:0:12}" 'substr($2,1,12)!=id {print $1}')
  if [[ -n "$stale" ]]; then
    echo "NOTE: still running on the old image (restart to pick up the new one):"
    echo "$stale" | sed 's/^/  /'
  fi
done

echo
echo "done. If a verb was added, remember verbs.toml ships it OFF — flipping"
echo "it on is a separate, deliberate sudoedit of /etc/disjorn-broker/verbs.toml."
