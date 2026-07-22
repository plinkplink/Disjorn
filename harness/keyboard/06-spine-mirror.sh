#!/usr/bin/env bash
# 06-spine-mirror.sh — publish a resident's spine to a res-readable,
# resident-UNWRITABLE mirror under /srv. Run by plink, with sudo.
#
# WHY THIS EXISTS (protection by placement).
# A resident's spine is the directory house_memory/bootstrap.py assembles
# into ~/.claude/CLAUDE.md at the start of EVERY session — the kernel.
# AGENTHOOD.md rules that a resident's own code and prompt are always
# Tier 2 (a human reviews every change), and bootstrap.py's docstring
# assumes a spine edit arrives "witnessed, merged".
#
# That only holds if the spine the container loads is not writable by the
# resident. Before this script, gable's env file said
#   RESIDENT_SPINE_DIR=/home/resident/bots/fable/spine
# which is the host path /home/res-gable/resident-home/bots/fable/spine —
# inside his own read-write home volume, owned by res-gable. He could
# rewrite his own kernel and the next summon would load it: no diff, no
# classifier, no #custodian, no human. The WP-H4 classifier catches
# SUBMITTED diffs; it cannot see a direct write. Placement is the wall.
#
# The canonical spine stays where it is — /home/plink/bots/<name>/spine,
# plink-owned, inside 0700 /home/plink, which res-* users cannot traverse
# (that is why the writable in-volume copy got used in the first place).
# This script publishes a COPY to /srv/disjorn-spine/<name>, world-readable
# and owned by plink, in exactly the spirit of /srv/disjorn-ro and
# /srv/disjorn-resident-config.
#
# NEVER solve the traversal problem by loosening /home/plink or the
# canonical spine. Copy outward; do not open inward.
#
# Usage:
#   sudo bash 06-spine-mirror.sh <resident> [canonical-spine-dir]
#     <resident>              e.g. "gable" (no res- prefix)
#     [canonical-spine-dir]   default /home/plink/bots/<resident>/spine,
#                             except gable -> /home/plink/bots/fable/spine
#                             (his repo kept its pre-rename name)
#
# Env overrides (tests use these; production uses the defaults):
#   SPINE_MIRROR_ROOT   default /srv/disjorn-spine
#   SPINE_OWNER         default plink:plink   (matches /srv/disjorn-ro)
#
# WHO CONSUMES THE MIRROR — two things, same directory:
#   1. The resident container. run-resident.sh / run-build.sh mount
#      $RESIDENT_SPINE_HOST read-only at /opt/spine; plink then points
#      RESIDENT_SPINE_DIR=/opt/spine in the /config env file. See
#      harness/cc/config-template/README.md § Spine placement.
#   2. The consolidation job (harness/consolidation), which reads the spine
#      as the res-* uid and is blocked today precisely because the
#      canonical copy is un-traversable. Point [spine].dir at the mirror.
#
# REFRESH DISCIPLINE. This script is the ONLY way an approved spine change
# reaches a resident. The flow is:
#   resident proposes  ->  broker file-proposal  ->  #custodian  ->  plink
#   reviews and edits /home/plink/bots/<name>/spine  ->  THIS SCRIPT  ->
#   next session loads it.
# Re-running is safe and idempotent; it also DELETES mirror entries whose
# canonical counterpart is gone, so an approved deletion propagates.
set -euo pipefail

NAME="${1:?usage: 06-spine-mirror.sh <resident> [canonical-spine-dir]}"

case "$NAME" in
  gable) _default_src="/home/plink/bots/fable/spine" ;;   # pre-rename repo
  *)     _default_src="/home/plink/bots/$NAME/spine" ;;
esac
SRC="${2:-$_default_src}"

MIRROR_ROOT="${SPINE_MIRROR_ROOT:-/srv/disjorn-spine}"
OWNER="${SPINE_OWNER:-plink:plink}"
DST="$MIRROR_ROOT/$NAME"

die() { echo "06-spine-mirror: $*" >&2; exit 1; }

[ -d "$SRC" ] || die "canonical spine not a directory: $SRC"

# Fail loud on an empty source rather than publishing an empty kernel: a
# resident whose spine assembles to nothing is a resident with no kernel,
# and bootstrap.py would exit 2 mid-summon. Refuse here instead.
shopt -s nullglob
entries=( "$SRC"/*.md )
shopt -u nullglob
[ "${#entries[@]}" -gt 0 ] || die "canonical spine has no *.md entries: $SRC"

# Only *.md is published. The spine is markdown; anything else in that
# directory (a stray script, an editor backup) has no business being
# readable by a resident uid, and silently shipping it would be exactly the
# kind of quiet widening this file exists to prevent.
for f in "$SRC"/* "$SRC"/.[!.]*; do
  [ -e "$f" ] || continue
  case "$f" in
    *.md) ;;
    *) echo "06-spine-mirror: NOTE not published (only *.md is): $f" >&2 ;;
  esac
done

echo "06-spine-mirror: $SRC -> $DST (${#entries[@]} entries, owner $OWNER)"

install -d -o "${OWNER%%:*}" -g "${OWNER##*:}" -m 0755 "$MIRROR_ROOT"
install -d -o "${OWNER%%:*}" -g "${OWNER##*:}" -m 0755 "$DST"

for f in "${entries[@]}"; do
  install -o "${OWNER%%:*}" -g "${OWNER##*:}" -m 0644 "$f" "$DST/$(basename "$f")"
done

# Prune: an entry plink deleted from the canonical spine must disappear
# from the mirror too, or the resident keeps loading a retired kernel line.
for f in "$DST"/*; do
  [ -e "$f" ] || continue
  b="$(basename "$f")"
  if [ ! -e "$SRC/$b" ]; then
    echo "06-spine-mirror: pruning retired entry: $b"
    rm -f -- "$f"
  fi
done

# Verify what we just published, loudly. These are the two properties the
# whole exercise is for; assert them rather than trusting the mode bits we
# just asked for.
bad=0
while IFS= read -r p; do
  echo "06-spine-mirror: PERMISSION PROBLEM (group/other-writable): $p" >&2
  bad=1
done < <(find "$DST" -perm /022 2>/dev/null)
[ "$bad" -eq 0 ] || die "mirror is writable by non-owners — refusing to call this done"

echo "06-spine-mirror: published:"
ls -l "$DST"
cat <<EOF

Next steps (plink, deliberate — this script changes NO live config):
  * point the container at it, once:
      RESIDENT_SPINE_HOST=$DST      (systemd unit Environment=, host-side)
      RESIDENT_SPINE_DIR=/opt/spine (/config env file, container-side)
    see harness/cc/config-template/README.md § Spine placement
  * point consolidation at it:
      [spine] dir = "$DST"          in harness/consolidation/config/$NAME.toml
  * verify it is unwritable from the resident uid:
      sudo -u res-$NAME test -w $DST && echo BAD || echo "good: not writable"
EOF
