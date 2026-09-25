#!/usr/bin/env bash
# Restore the newest snapshot into scratch, check it against its own manifest, post pass/fail.
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

need restic sqlite3 git python3

scratch="$(mktemp -d "${TMPDIR:-/var/tmp}/disjorn-drill.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
fails=()
fail() { fails+=("$1"); }

if ! latest="$(latest_snapshot)"; then
    printf 'backup drill RED: restic snapshots failed\n' | post
    exit 1
fi
read -r snap _ <<<"$latest"
if [[ -z "${snap:-}" ]]; then
    printf 'backup drill RED: no %s snapshot in the repo\n' "$BACKUP_TAG" | post
    exit 1
fi
if ! restic restore --retry-lock 30m "$snap" --target "$scratch/root" >/dev/null; then
    printf 'backup drill RED: restic restore of %s failed\n' "${snap:0:8}" | post
    exit 1
fi

r="$scratch/root"
stage="$r$STAGE"
m="$stage/manifest.json"
if [[ ! -f "$m" ]]; then
    printf 'backup drill RED: snapshot %s has no manifest\n' "${snap:0:8}" | post
    exit 1
fi

db="$stage/disjorn.db"
ic="$(sqlite3 "$db" 'PRAGMA integrity_check' 2>&1)"
[[ "$ic" == ok ]] || fail "db integrity_check: ${ic:0:200}"
messages="$(sqlite3 "$db" 'SELECT COUNT(*) FROM messages' 2>&1)"
[[ "$messages" == "$(mget "$m" messages)" ]] || fail "db messages $messages != manifest $(mget "$m" messages)"
migration="$(sqlite3 "$db" 'SELECT MAX(filename) FROM schema_migrations' 2>&1)"
[[ "$migration" =~ ^0*([0-9]+) && "${BASH_REMATCH[1]}" == "$(mget "$m" migration)" ]] \
    || fail "db migration $migration != manifest $(mget "$m" migration)"

c_count="$(mget "$m" claudette.count)"
if ! out="$("$CLAUDETTE_PY" "$HERE/memory_export.py" verify \
        --export "$stage/claudette/memory-export.json" \
        --count "$c_count" --sha "$(mget "$m" claudette.export_sha256)" \
        --scratch "$scratch/claudette-store" 2>&1)"; then
    fail "claudette: ${out//$'\n'/; }"
fi

if facts="$(gable_facts "$r$GABLE_MEMORY" 2>&1)"; then
    read -r g_files g_sha <<<"$facts"
    [[ "$g_files" == "$(mget "$m" gable.memory_files)" ]] \
        || fail "gable memory files $g_files != manifest $(mget "$m" gable.memory_files)"
    [[ "$g_sha" == "$(mget "$m" gable.memory_md_sha256)" ]] || fail "gable MEMORY.md sha differs from manifest"
else
    fail "gable: $facts"
fi

git init -q --bare "$scratch/verify.git"
n_bundles=0
for pair in "${BUNDLES[@]}"; do
    name="${pair%%=*}"
    b="$stage/bundles/$name.bundle"
    n_bundles=$((n_bundles + 1))
    git -C "$scratch/verify.git" bundle verify -q "$b" >/dev/null 2>&1 || { fail "bundle $name: verify failed"; continue; }
    heads="$(git bundle list-heads "$b" | LC_ALL=C sort -k2,2)"
    [[ "$heads" == "$(mget "$m" "bundles.$name")" ]] || fail "bundle $name: branch HEADs differ from manifest"
done

if ((${#fails[@]})); then
    { printf 'backup drill RED on snapshot %s (%d failed):\n' "${snap:0:8}" "${#fails[@]}"
      printf -- '- %s\n' "${fails[@]}"; } | post
    exit 1
fi
printf 'backup drill PASS on snapshot %s (%s): db ok, %s messages, migration %s; claudette %s memories round-trip; gable %s memory files; %d bundles verified\n' \
    "${snap:0:8}" "$(mget "$m" created_at)" "$messages" "$(mget "$m" migration)" \
    "$c_count" "$g_files" "$n_bundles" | post
