#!/usr/bin/env bash
# Daily: post red if the newest snapshot is older than FRESH_HOURS (or missing).
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

need restic python3

latest="$(latest_snapshot)"
read -r snap age <<<"$latest" || true
if [[ -z "${snap:-}" ]]; then
    printf 'backup freshness RED: no %s snapshot in the repo\n' "$BACKUP_TAG" | post
    exit 1
fi
if python3 -c 'import sys; sys.exit(float(sys.argv[1]) > float(sys.argv[2]))' "$age" "$FRESH_HOURS"; then
    echo "newest snapshot ${snap:0:8} is ${age}h old"
    exit 0
fi
printf 'backup freshness RED: newest snapshot %s is %sh old (limit %sh); the nightly is not landing\n' \
    "${snap:0:8}" "$age" "$FRESH_HOURS" | post
exit 1
