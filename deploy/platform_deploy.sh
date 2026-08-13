#!/usr/bin/env bash
# merge-loop — review, merge, build, propagate a build branch.
# usage: merge-loop <slug> [confirm-seq]     e.g. merge-loop 2026-08-06-seq-hover 776
set -euo pipefail
slug="${1:?usage: merge-loop <slug> [confirm-seq]}"; seq="${2:-}"
repo="$HOME/Disjorn/Disjorn"
gh="/var/lib/disjorn-broker/gatehouse/disjorn.git"

git -C "$repo" fetch "$gh" "loop/$slug"
git -C "$repo" diff --stat main...FETCH_HEAD
git -C "$repo" diff main...FETCH_HEAD | ${PAGER:-less}
read -rp "merge loop/$slug? [y/N] " ok; [ "$ok" = y ] || exit 1

git -C "$repo" merge --no-ff FETCH_HEAD \
  -m "merge: $slug (SPECS/$slug.md${seq:+, confirmed seq $seq})"
if git -C "$repo" diff --name-only HEAD^ HEAD | grep -q '^client/'; then
  (cd "$repo/client" && npm run typecheck && npm run build)
fi
if git -C "$repo" diff --name-only HEAD^ HEAD | grep -q '^server/'; then
  echo "NOTE: server/ changed — restart ritual applies; this script does not touch it."
fi
git -C "$repo" push origin main
git -C "$repo" push "$gh" main
echo "done — a resident runs: broker refresh-mirror"