# Sourced by snapshot.sh, drill.sh, freshness.sh and the alert unit.
# Every path is overridable from the environment; the defaults are host-absolute.

: "${DISJORN:=/home/plink/Disjorn/Disjorn}"
: "${DB:=$DISJORN/server/data/disjorn.db}"
: "${CLAUDETTE_HOME:=/home/res-claudette/resident-home}"
: "${GABLE_HOME:=/home/res-gable/resident-home}"
: "${GABLE_MEMORY:=$GABLE_HOME/.claude/projects/-home-resident/memory}"
: "${CLAUDETTE_REPO:=/home/plink/bots/claudette}"
: "${SPINE_REPO:=/home/plink/bots/fable/spine}"
: "${CLAUDETTE_DISCORD_MEMORY_DIR:=$CLAUDETTE_REPO/chroma_data}"
: "${CLAUDETTE_PY:=/home/plink/bots/claudette/.venv/bin/python}"
: "${POST_PY:=$DISJORN/server/.venv/bin/python}"
: "${BROKER_CONFIG:=/etc/disjorn-broker/broker.toml}"
: "${STAGE:=/var/lib/disjorn-backup/stage}"
: "${BACKUP_TAG:=disjorn-nightly}"
: "${FRESH_HOURS:=26}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# name=repo pairs; the bundle file is $STAGE/bundles/<name>.bundle.
BUNDLES=("claudette=$CLAUDETTE_REPO" "spine=$SPINE_REPO" "disjorn=$DISJORN")

default_paths() {
    printf '%s\n' \
        "$STAGE" \
        "$DISJORN/server/data/uploads" \
        "$DISJORN/server/data/assets" \
        "$DISJORN/server/.env" \
        "$CLAUDETTE_HOME" \
        "$GABLE_HOME" \
        /var/lib/disjorn-broker \
        /var/log/disjorn-broker \
        /etc/disjorn-broker \
        /home/plink/resident-config \
        /srv/disjorn-resident-config
}

# BACKUP_PATHS (newline-separated) replaces the default list.
backup_paths() {
    if [[ -n "${BACKUP_PATHS:-}" ]]; then printf '%s\n' "$BACKUP_PATHS"; else default_paths; fi
}

git_() { git -c safe.directory='*' "$@"; }

# `.backup` of a live sqlite database as its owner: sqlite may create -wal/-shm
# beside the source, and root-owned ones lock that owner out of it.
backup_live_db() {
    local src="$1" dest="$2" owner tmp
    if [[ "$(id -u)" == "$(stat -c %u "$src")" ]]; then
        sqlite3 "$src" ".backup '$dest'"
        return
    fi
    need runuser
    owner="$(stat -c %U "$src")"
    tmp="$(mktemp -d)"
    chown "$owner" "$tmp"
    if ! runuser -u "$owner" -- sqlite3 "$src" ".backup '$tmp/copy.db'"; then
        rm -rf "$tmp"
        return 1
    fi
    mv "$tmp/copy.db" "$dest"
    rm -rf "$tmp"
}

need() {
    local t
    for t in "$@"; do
        command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 1; }
    done
}

# Body on stdin -> #custodian as the broker's bot. BACKUP_POST_CMD replaces the transport.
post() {
    if [[ -n "${BACKUP_POST_CMD:-}" ]]; then
        bash -c "$BACKUP_POST_CMD"
        return
    fi
    local body
    body="$(cat)"
    "$POST_PY" - "$BROKER_CONFIG" "$DISJORN/harness/broker" "$body" <<'PY'
import sys, tomllib
cfg_path, broker_dir, body = sys.argv[1:4]
sys.path.insert(0, broker_dir)
from brokerd import _sdk_transport
with open(cfg_path, "rb") as fh:
    cfg = tomllib.load(fh)
print(_sdk_transport(cfg.get("disjorn", {}), body))
PY
}

# Print manifest field at a dotted path; objects print as sorted "value key" lines.
mget() {
    python3 - "$1" "$2" <<'PY'
import json, sys
v = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    v = v[k]
if isinstance(v, dict):
    print("\n".join(f"{v[k]} {k}" for k in sorted(v)))
else:
    print(v)
PY
}

gable_facts() {
    python3 - "$1" <<'PY'
import hashlib, pathlib, sys
d = pathlib.Path(sys.argv[1])
if not d.is_dir():
    sys.exit(f"no memory dir at {d}")
n = sum(1 for p in d.rglob("*") if p.is_file())
print(n, hashlib.sha256((d / "MEMORY.md").read_bytes()).hexdigest())
PY
}

# Newest snapshot with our tag as "<id> <age-hours>"; empty if there is none.
latest_snapshot() {
    restic snapshots --json --tag "$BACKUP_TAG" --retry-lock 30m | python3 -c '
import datetime as dt, json, re, sys
snaps = json.load(sys.stdin) or []
def when(s):
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$", s["time"])
    return dt.datetime.fromisoformat(m.group(1) + m.group(2).replace("Z", "+00:00"))
if snaps:
    s = max(snaps, key=when)
    age = (dt.datetime.now(dt.timezone.utc) - when(s)).total_seconds() / 3600
    print(s["id"], f"{age:.1f}")
'
}
