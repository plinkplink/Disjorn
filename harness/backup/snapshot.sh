#!/usr/bin/env bash
# Nightly: stage the DB copy, Claudette's export, git bundles, the apps and the manifest, then restic backup + prune.
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

need restic sqlite3 git python3
: "${CLAUDETTE_MEMORY_DIR:?set in the backup env file}"
: "${CLAUDETTE_RETRIEVAL_LOG:?set in the backup env file}"

rm -rf "$STAGE"
mkdir -p -m 0700 "$STAGE"
mkdir -p "$STAGE/bundles" "$STAGE/claudette"

backup_live_db "$DB" "$STAGE/disjorn.db"
messages="$(sqlite3 "$STAGE/disjorn.db" 'SELECT COUNT(*) FROM messages')"
migration="$(sqlite3 "$STAGE/disjorn.db" 'SELECT MAX(filename) FROM schema_migrations')"

# Never open a live chroma dir as root: files chroma creates there lock its owner out.
export_store() {
    local src="$1" prefix="$2" copy="$STAGE/claudette/chroma-copy"
    cp -a "$src" "$copy"
    rm -f "$copy"/chroma.sqlite3*
    backup_live_db "$src/chroma.sqlite3" "$copy/chroma.sqlite3"
    "$CLAUDETTE_PY" "$HERE/memory_export.py" export \
        --data-dir "$copy" --out "$STAGE/claudette/${prefix}memory-export.json" \
        > "$STAGE/claudette/${prefix}export.json"
    rm -rf "$copy"
}
export_store "$CLAUDETTE_MEMORY_DIR" ""
if [[ -f "$CLAUDETTE_DISCORD_MEMORY_DIR/chroma.sqlite3" ]]; then
    export_store "$CLAUDETTE_DISCORD_MEMORY_DIR" discord-
fi
cp "$CLAUDETTE_RETRIEVAL_LOG" "$STAGE/claudette/memory_retrieval.jsonl"

for pair in "${BUNDLES[@]}"; do
    name="${pair%%=*}"
    git_ -C "${pair#*=}" bundle create -q "$STAGE/bundles/$name.bundle" --all
    git bundle list-heads "$STAGE/bundles/$name.bundle" > "$STAGE/bundles/$name.heads"
done

python3 "$HERE/apps.py" stage --git-root "$APPS_GIT_ROOT" --www-root "$APPS_WWW_ROOT" --out "$STAGE/apps" \
    --timeout "$BACKUP_CHILD_TIMEOUT"

facts="$(gable_facts "$GABLE_MEMORY")"
read -r gable_files gable_sha <<<"$facts"

python3 - "$STAGE" "$messages" "$migration" "$gable_files" "$gable_sha" <<'PY'
import datetime as dt, json, pathlib, re, sys
stage, messages, migration, gable_files, gable_sha = sys.argv[1:6]
stage = pathlib.Path(stage)
bundles = {}
for heads in sorted((stage / "bundles").glob("*.heads")):
    pairs = (line.split(" ", 1) for line in heads.read_text().splitlines())
    bundles[heads.stem] = {ref: sha for sha, ref in pairs}
manifest = {
    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    "messages": int(messages),
    "migration": int(re.match(r"\d+", migration).group()),
    "bundles": bundles,
    "claudette": json.loads((stage / "claudette" / "export.json").read_text()),
    "claudette_discord": (json.loads(discord.read_text())
                          if (discord := stage / "claudette" / "discord-export.json").exists()
                          else None),
    "gable": {"memory_files": int(gable_files), "memory_md_sha256": gable_sha},
    "apps": json.loads((stage / "apps" / "apps.json").read_text()),
}
(stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY

mapfile -t paths < <(backup_paths)
tags="$BACKUP_TAG"
lost_roots="$(mget "$STAGE/manifest.json" apps.lost_roots)"
[[ -z "$(mget "$STAGE/manifest.json" apps.warnings)$lost_roots" ]] || tags+=",apps-partial"
restic backup --retry-lock 30m --tag "$tags" "${paths[@]}"
restic forget --retry-lock 30m --tag "$BACKUP_TAG" --prune \
    --keep-daily 7 --keep-weekly 4 --keep-monthly 12
rm -rf "$STAGE"
if [[ -n "$lost_roots" ]]; then
    printf 'snapshot taken WITHOUT the apps: an apps root could not be read (%s)\n' "${lost_roots//$'\n'/; }" >&2
    exit 1
fi
