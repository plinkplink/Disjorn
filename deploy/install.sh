#!/usr/bin/env bash
# Disjorn convenience installer — venv + pip + client build. Idempotent:
# safe to re-run; reuses an existing .venv / node_modules. Makes NO systemd
# changes and never overwrites an existing server/.env.
#
# Usage:  ./deploy/install.sh [--ml]
#   --ml   also install requirements-ml.txt (faster-whisper + rawpy; heavy —
#          intended for the GPU box).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$REPO_ROOT/server"
CLIENT_DIR="$REPO_ROOT/client"
VENV="$SERVER_DIR/.venv"
INSTALL_ML=false

for arg in "$@"; do
    case "$arg" in
        --ml) INSTALL_ML=true ;;
        *) echo "unknown option: $arg (only --ml is supported)" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }

# --- Prerequisite checks ----------------------------------------------------
missing=()
command -v python3 >/dev/null || missing+=("python3")
command -v npm >/dev/null || missing+=("npm (nodejs)")
python3 -c 'import venv' 2>/dev/null || missing+=("python3-venv")
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Missing prerequisites: ${missing[*]}" >&2
    echo "On Debian: sudo apt install python3 python3-venv nodejs npm" >&2
    exit 1
fi

# --- Python venv + deps -----------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    step "Creating virtualenv at server/.venv"
    python3 -m venv "$VENV"
else
    step "Reusing existing virtualenv at server/.venv"
fi

step "Installing Python dependencies"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$SERVER_DIR/requirements.txt"

if $INSTALL_ML; then
    step "Installing ML extras (faster-whisper, rawpy) — this can take a while"
    "$VENV/bin/pip" install --quiet -r "$SERVER_DIR/requirements-ml.txt"
fi

# --- Client build -----------------------------------------------------------
step "Installing client dependencies (npm ci)"
(cd "$CLIENT_DIR" && npm ci --no-audit --no-fund)

step "Building client (npm run build) -> client/dist"
(cd "$CLIENT_DIR" && npm run build)

# --- .env -------------------------------------------------------------------
if [ ! -f "$SERVER_DIR/.env" ]; then
    step "Creating server/.env from deploy/.env.example"
    cp "$REPO_ROOT/deploy/.env.example" "$SERVER_DIR/.env"
    ENV_CREATED=true
else
    step "Keeping existing server/.env"
    ENV_CREATED=false
fi

# --- Next steps -------------------------------------------------------------
cat <<EOF

Done. Next steps (see deploy/README-DEPLOY.md for details):

  1. Edit server/.env:
       - set SECRET_KEY:  python3 -c "import secrets; print(secrets.token_urlsafe(48))"
       - VAPID keys:      cd server && .venv/bin/python cli.py gen-vapid
$( $ENV_CREATED || echo "     (server/.env already existed — verify it is complete against deploy/.env.example)" )
  2. Create accounts:
       cd server
       .venv/bin/python cli.py create-user <name> --admin
       .venv/bin/python cli.py create-bot <name>
  3. Install the systemd unit (edit paths/User first if needed):
       sudo cp deploy/disjorn.service /etc/systemd/system/
       sudo systemctl daemon-reload && sudo systemctl enable --now disjorn
EOF
