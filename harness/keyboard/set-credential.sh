#!/usr/bin/env bash
# set-credential.sh — put a minted credential into one seat's env file.
#
# RUN BY: plink, at the keyboard, after `claude setup-token`:
#
#     claude setup-token                       # prints a token
#     bash harness/keyboard/set-credential.sh chat claudette
#     <paste the token, press Ctrl-D>
#
# WHY A SCRIPT AND NOT AN EDITOR. Four things have to be right at once and
# three of them are invisible in an editor: the file's owner (plink), its group
# (the res-* uid that reads it), its mode (0640 — the resident must read it and
# nobody else), and the ABSENCE of the other credential. That last one is the
# one that bites: run-resident.sh PREFERS CLAUDE_CODE_OAUTH_TOKEN whenever it is
# present, so leaving an API key behind in an account-routed seat does nothing
# visible, while leaving an OAuth token behind in a key-routed seat silently
# keeps spending the account. A seat holds exactly one credential; this script
# is what makes that true rather than hoped.
#
# THE TOKEN ARRIVES ON STDIN, NEVER IN ARGV — same doctrine as every other
# credential path here. An argument is visible in /proc/*/cmdline to every user
# on the host and lands in shell history; stdin does neither.
set -euo pipefail

SEAT="${1:-}"
NAME="${2:-}"
case "$SEAT" in
  chat)  ROOT=/srv/disjorn-resident-config ;;
  build) ROOT=/srv/disjorn-build-config ;;
  *) echo "usage: set-credential.sh <chat|build> <resident> [--key]" >&2; exit 2 ;;
esac
[ -n "$NAME" ] || { echo "usage: set-credential.sh <chat|build> <resident> [--key]" >&2; exit 2; }

# Default is the Max account token. `--key` writes a metered API key instead,
# for the day a seat is deliberately routed back to metered billing.
VAR=CLAUDE_CODE_OAUTH_TOKEN
[ "${3:-}" = "--key" ] && VAR=ANTHROPIC_API_KEY
OTHER=ANTHROPIC_API_KEY
[ "$VAR" = "ANTHROPIC_API_KEY" ] && OTHER=CLAUDE_CODE_OAUTH_TOKEN

ENV_FILE="$ROOT/$NAME/env"
[ -f "$ENV_FILE" ] || { echo "no such seat: $ENV_FILE" >&2; exit 1; }
RES_USER="res-$NAME"
id "$RES_USER" >/dev/null 2>&1 || { echo "no such user: $RES_USER" >&2; exit 1; }

echo "Paste the credential for the $SEAT seat of $NAME, then Ctrl-D:" >&2
TOKEN="$(cat)"
TOKEN="${TOKEN//[$'\t\r\n ']/}"
[ -n "$TOKEN" ] || { echo "nothing read on stdin; not touching $ENV_FILE" >&2; exit 1; }

BACKUP_DIR=/home/plink/cred-backups
install -d -m 0700 -o root -g root "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
cp -a "$ENV_FILE" "$BACKUP_DIR/$SEAT-$NAME-env.$STAMP"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
# Drop BOTH credential lines, then write exactly the one this seat should hold.
grep -vE "^($VAR|$OTHER)=" "$ENV_FILE" > "$TMP" || true
printf '%s=%s\n' "$VAR" "$TOKEN" >> "$TMP"

install -m 0640 -o plink -g "$RES_USER" "$TMP" "$ENV_FILE"

echo >&2
echo "$ENV_FILE now holds: $(grep -oE '^[A-Z_]+' "$ENV_FILE" | sort | tr '\n' ' ')" >&2
echo "  owner/mode: $(stat -c '%U:%G %a' "$ENV_FILE")" >&2
echo "  backup:     $BACKUP_DIR/$SEAT-$NAME-env.$STAMP" >&2
if [ "$SEAT" = chat ] && [ "$NAME" = claudette ]; then
  echo >&2
  echo "Claudette's chat container is LONG-RUNNING and read its env at start." >&2
  echo "It keeps the old credential until you restart it:" >&2
  echo "  sudo systemctl --user -M res-claudette@ restart resident-cc" >&2
fi
