#!/usr/bin/env bash
# 11-apps-gitdir-migrate.sh — move every app's git dir OUT of the container
# mount (SPECS/2026-09-13-apps-gitdir-outside-the-mount.md, D6).
#
# RUN BY: plink, at his keyboard, with sudo, ONCE, with apps IDLE:
#
#     sudo bash harness/keyboard/11-apps-gitdir-migrate.sh            # do it
#     sudo bash harness/keyboard/11-apps-gitdir-migrate.sh --audit    # look only
#
# WHAT CHANGED AND WHY. Until this script runs, an app's repository is at
# /srv/apps/<id>/.git — inside the rw mount a builder turn writes as /work. So
# the turn wrote the metadata that every host-side git command then read:
# `core.fsmonitor` in that config was HOST CODE EXECUTION as res-appsbuilding
# under the harvest's own `git add -A` (Gable #2546), and an
# `objects/info/alternates` plus a symlinked ref made `remix` hand over another
# app's entire history (#2557). After this script the repository is at
# /srv/apps-git/<id>.git, which is mounted into no container, ever.
#
# THE AUDIT IS THE POINT, NOT THE MOVE. "Was this hole ever used?" is a
# question that can be answered TODAY and never again after the rewrite below,
# so every app's block is printed BEFORE anything of that app's moves: every
# config key outside the handful this house writes, every non-sample hook,
# every alternates file, every symlink anywhere under .git. KEEP THE OUTPUT —
# it goes in the merge record. It is the only copy.
#
# IDEMPOTENT. An app whose work tree holds no `.git` is left alone (its first
# turn creates the git dir); a second run of this script is a no-op that says
# so. It REFUSES while any disjorn-apps-* unit is active: a turn mid-harvest
# whose repository moves out from under it is a lost turn, and there is no
# hurry that is worth that.
#
# IT NEVER RE-INITS OVER HISTORY. Every branch that cannot keep an app's
# history QUARANTINES the whole old .git first (0700, under
# /srv/apps-quarantine/<id>/gitdir-migration/) and only then makes a fresh
# repository. Nothing here deletes a git dir. The only files this script
# removes are inside a git dir it has already moved: hooks/, the config it
# replaces, and objects/info/alternates.
set -euo pipefail

# Overridable ONLY so the test suite can run the real script against a scratch
# tree as an unprivileged user — the same discipline 08-gatehouse-repo.sh and
# 10-appsbuilding.sh use. On this box every one of them is the default, and the
# root check below is skipped only when EVERY root has been pointed elsewhere.
APPS_ROOT="${APPS_ROOT:-/srv/apps}"
GIT_ROOT="${GIT_ROOT:-/srv/apps-git}"
QUARANTINE_ROOT="${QUARANTINE_ROOT:-/srv/apps-quarantine}"
SEAT="${SEAT:-res-appsbuilding}"
SYSTEMCTL="${SYSTEMCTL:-/usr/bin/systemctl}"
GIT="${GIT:-/usr/bin/git}"
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
# The INSTALLED harvest, because that is the module that will run the next
# turn and the fresh repositories this script makes have to be the shape it
# expects (ensure_repo's end state, D3). The repo copy is a fallback so a
# keyboard run before the install still works, and it says which it used.
HARVEST="${HARVEST:-/usr/local/lib/disjorn/apps_harvest.py}"
[ -f "$HARVEST" ] || HARVEST="$REPO_DIR/harness/cc/apps/apps_harvest.py"
UNIT_GLOB='disjorn-apps-*.service'

# The four keys this house's own repositories carry, plus the two identity
# tables. ANYTHING ELSE in a config is printed verbatim by the audit, because
# anything else was written by something that is not this house.
OURS_RE='^(core\.(repositoryformatversion|filemode|bare|logallrefupdates)|user\.[^=]+|init\.[^=]+)='

AUDIT_ONLY=0
[ "${1:-}" = "--audit" ] && AUDIT_ONLY=1

say()  { echo "== $*"; }
note() { echo "   $*"; }
die()  { echo "11-apps-gitdir-migrate: FATAL: $*" >&2; exit 1; }

# No sudo needed for an entirely-scratch run; everything else writes /srv.
if [ "$(id -u)" != 0 ] \
   && { [ "$APPS_ROOT" = /srv/apps ] || [ "$GIT_ROOT" = /srv/apps-git ] \
        || [ "$QUARANTINE_ROOT" = /srv/apps-quarantine ]; }; then
  die "run with sudo: sudo bash harness/keyboard/11-apps-gitdir-migrate.sh"
fi

# `install -o/-g` only works as root; a scratch run owns its own files already.
own() {                      # own <mode> <path>
  chmod "$1" "$2"
  [ "$(id -u)" = 0 ] && chown -R "$SEAT:$SEAT" "$2"
  return 0
}

# ── the refusal ─────────────────────────────────────────────────────────────
# A turn's harvest holds the repository open for the length of a commit and an
# rsync. Moving it mid-turn costs that turn its record.
active="$("$SYSTEMCTL" list-units --no-legend --no-pager --plain --state=active \
          "$UNIT_GLOB" 2>/dev/null | awk '{print $1}' | grep . || true)"
if [ -n "$active" ]; then
  die "apps units are ACTIVE — migrate when the seat is idle:
$active"
fi

[ -d "$APPS_ROOT" ] || die "no $APPS_ROOT — nothing to migrate"
mkdir -p "$GIT_ROOT"
own 0750 "$GIT_ROOT"

# ── the audit (D6.1) ────────────────────────────────────────────────────────
# ONE BLOCK PER APP, PRINTED BEFORE THAT APP MOVES. The `git config --list`
# below is one of the two git calls in this file that does not name a
# --git-dir, and it is deliberate: it reads a config that has NOT moved yet,
# by --file, with --no-includes so an `[include] path = ...` the turn wrote
# cannot make this print a file that is none of its business. It is named by
# name in harness/cc/tests/test_apps_launch.py's grep pin.
audit_app() {                # audit_app <app-id> <dot-git>
  local id="$1" dot="$2" out
  say "audit $id ($dot)"
  if [ -L "$dot" ]; then
    note "SHAPE: a SYMLINK -> $(readlink "$dot")   [history will be discarded]"
    return 0
  fi
  if [ ! -d "$dot" ]; then
    note "SHAPE: a FILE, not a directory   [history will be discarded]"
    note "content: $(head -c 200 "$dot" | tr -d '\0' | tr '\n' ' ')"
    return 0
  fi
  note "SHAPE: a real directory"

  if [ -f "$dot/config" ]; then
    out="$("$GIT" config --file "$dot/config" --no-includes --list 2>/dev/null \
           | grep -Ev "$OURS_RE" || true)"
    if [ -n "$out" ]; then
      note "CONFIG KEYS THIS HOUSE DOES NOT WRITE:"
      printf '%s\n' "$out" | sed 's/^/     /' 
    else
      note "config: only this house's own keys"
    fi
  else
    note "config: absent"
  fi

  out="$(find "$dot/hooks" -maxdepth 1 -type f ! -name '*.sample' 2>/dev/null || true)"
  if [ -n "$out" ]; then
    note "HOOKS THAT ARE NOT SAMPLES:"
    printf '%s\n' "$out" | sed 's/^/     /' 
  else
    note "hooks: samples only"
  fi

  if [ -e "$dot/objects/info/alternates" ]; then
    note "ALTERNATES:"
    sed 's/^/     /' "$dot/objects/info/alternates"
  else
    note "alternates: none"
  fi

  out="$(find "$dot" -type l -printf '%p -> %l\n' 2>/dev/null || true)"
  if [ -n "$out" ]; then
    note "SYMLINKS UNDER .git:"
    printf '%s\n' "$out" | sed 's/^/     /'
  else
    note "symlinks: none"
  fi
}

# ── the branches (D6.2 – D6.4) ──────────────────────────────────────────────

quarantine_gitdir() {        # quarantine_gitdir <app-id> <dot-git>
  local id="$1" dot="$2" dest="$QUARANTINE_ROOT/$1/gitdir-migration"
  mkdir -p "$dest"
  chmod 0700 "$QUARANTINE_ROOT/$1" "$dest"
  # `mv`, never `cp -r`: a pointer file or a symlink is MOVED as itself and
  # nothing here ever follows one. A second run finds the destination taken and
  # stamps the new one, because losing the first quarantine would lose the
  # evidence this whole script is about.
  local target="$dest/.git"
  if [ -e "$target" ] || [ -L "$target" ]; then
    # A second quarantine of the same app. Losing the FIRST one would lose the
    # evidence this whole script exists to keep, so the new one is stamped.
    target="$dest/.git.$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  mv -- "$dot" "$target"
  [ "$(id -u)" = 0 ] && chown -R "$SEAT:$SEAT" "$QUARANTINE_ROOT/$1"
  note "quarantined -> $target"
}

fresh_repo() {               # fresh_repo <app-id> <work-tree> <why>
  local id="$1" work="$2" why="$3" g="$GIT_ROOT/$1.git"
  python3 "$HARVEST" ensure-repo "$g" "$work" "$GIT_ROOT" >/dev/null \
    || die "$id: could not initialise $g"
  "$GIT" --git-dir="$g" --work-tree="$work" add -A
  "$GIT" --git-dir="$g" --work-tree="$work" \
      -c user.name=apps-builder -c user.email=apps-builder@disjorn.local \
      commit --allow-empty -q -m "history discarded at migration: $why"
  own 0700 "$g"
  note "fresh repository at $g (history discarded: $why)"
}

sanitize_moved_gitdir() {    # sanitize_moved_gitdir <git-dir>
  local g="$1"
  # BEFORE ANY GIT COMMAND TOUCHES IT. The config is what makes a repository
  # dangerous (fsmonitor, clean filters, textconv, sshCommand), so it is not
  # edited key by key — a blocklist is a list git gets to extend — it is
  # DELETED and rewritten as the four lines this house writes.
  rm -f "$g/config"
  cat > "$g/config" <<'CONFIG_EOF'
[core]
	repositoryformatversion = 0
	bare = false
[user]
	name = apps-builder
	email = apps-builder@disjorn.local
CONFIG_EOF
  rm -rf "$g/hooks"
  rm -f "$g/objects/info/alternates"
}

migrate_app() {              # migrate_app <app-id>
  local id="$1" work="$APPS_ROOT/$1" dot="$APPS_ROOT/$1/.git" g="$GIT_ROOT/$1.git"

  if [ ! -e "$dot" ] && [ ! -L "$dot" ]; then
    say "$id: no .git in the work tree — nothing to migrate"
    return 0
  fi
  audit_app "$id" "$dot"
  [ "$AUDIT_ONLY" = 1 ] && return 0

  # ALREADY SPLIT. The repository is where it belongs and the work tree holds a
  # `.git` anyway — a stray a turn wrote after the split, or the second half of
  # an interrupted run of this script. The git dir is the app; the entry in the
  # work tree is evidence. (The harvest sweeps these every turn now; this is
  # the one that was there before the first post-split turn.)
  if [ -e "$g" ] || [ -L "$g" ]; then
    note "$g already exists — treating the work tree's .git as a stray"
    quarantine_gitdir "$id" "$dot"
    own 0700 "$g"
    return 0
  fi

  # D6.2 — not a real directory: a `gitdir:` pointer file or a symlink. NOT
  # FOLLOWED, moved as itself, history discarded.
  if [ -L "$dot" ] || [ ! -d "$dot" ]; then
    quarantine_gitdir "$id" "$dot"
    fresh_repo "$id" "$work" ".git was a pointer"
    return 0
  fi

  # D6.3 — a real directory with a symlink anywhere under it. A symlinked ref
  # or object path is the shape that made a clone read another app's history;
  # there is no sanitising that, so the whole thing is evidence.
  if [ -n "$(find "$dot" -type l -print -quit 2>/dev/null || true)" ]; then
    quarantine_gitdir "$id" "$dot"
    fresh_repo "$id" "$work" ".git held a symlink"
    return 0
  fi

  # D6.4 — the honest app. Move, sanitize, THEN fsck (fsck reads config).
  mv -- "$dot" "$g"
  sanitize_moved_gitdir "$g"
  if "$GIT" --git-dir="$g" fsck --no-dangling >/dev/null 2>&1; then
    own 0700 "$g"
    note "moved -> $g (config rewritten, hooks removed, alternates removed, fsck clean)"
  else
    note "FSCK FAILED on $g — the object store is not trustworthy; quarantining whole"
    quarantine_gitdir "$id" "$g"
    fresh_repo "$id" "$work" "fsck failed after the move"
  fi
}

# ── the run ─────────────────────────────────────────────────────────────────
say "apps root $APPS_ROOT -> git root $GIT_ROOT (quarantine $QUARANTINE_ROOT)"
say "fresh repositories come from $HARVEST"
[ "$AUDIT_ONLY" = 1 ] && say "--audit: reading only, nothing will move"
seen=0
for work in "$APPS_ROOT"/*; do
  [ -d "$work" ] || continue
  [ -L "$work" ] && { say "$(basename "$work"): a SYMLINK in $APPS_ROOT — skipped, look at it by hand"; continue; }
  id="$(basename "$work")"
  case "$id" in
    [a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7][a-z2-7]) ;;
    *) say "$id: not an app id (^[a-z2-7]{12}$) — skipped"; continue ;;
  esac
  seen=$((seen + 1))
  migrate_app "$id"
done
[ "$seen" = 0 ] && say "no apps under $APPS_ROOT"

echo
say "done. KEEP THE AUDIT BLOCKS ABOVE — they go in the merge record, and they"
say "are the only surviving copy of what was in those config files."
