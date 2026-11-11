#!/bin/sh
# entrypoint.sh: composes the seat's CLAUDE.md, then execs the runner.
# Image ENTRYPOINT: `podman run <image> claude -p ...` arrives here as "$@".
# $HOME is a per-turn tmpfs, empty at start, so CLAUDE.md is written every turn, never baked.
# Never put house instructions in /work: that is the user's repo.
# The composed file is read top to bottom: brief, ponytail, intensity line, shelf index.
set -eu

BRIEF="${APPS_BRIEF:-/config-image/BUILDER-BRIEF.md}"
SHELF="${APPS_SHELF:-/shelf}"
CLAUDE_HOME="${HOME:-/home/resident}/.claude"
MODE="${PONYTAIL_DEFAULT_MODE:-full}"

mkdir -p "$CLAUDE_HOME"
: > "$CLAUDE_HOME/CLAUDE.md"
chmod 0600 "$CLAUDE_HOME/CLAUDE.md"

if [ -f "$BRIEF" ]; then
  cat "$BRIEF" >> "$CLAUDE_HOME/CLAUDE.md"
else
  echo "apps-entrypoint: WARNING builder brief missing at $BRIEF" >&2
fi

# Ponytail is instructions only: no hooks, commands or MCP are vendored.
{
  echo
  echo "---"
  echo
  echo "## Ponytail — how much code to write"
  echo
} >> "$CLAUDE_HOME/CLAUDE.md"

if [ -f "$SHELF/ponytail/AGENTS.md" ]; then
  cat "$SHELF/ponytail/AGENTS.md" >> "$CLAUDE_HOME/CLAUDE.md"
  echo >> "$CLAUDE_HOME/CLAUDE.md"
else
  echo "apps-entrypoint: NOTE no ponytail at $SHELF/ponytail — brief composed without it" >&2
fi

# The intensity line is the mode, since the instruction-only install has no runtime switch.
# The lite/ultra text is quoted verbatim from the skill; a paraphrase flips their meaning.
case "$MODE" in
  off)
    echo "**Ponytail intensity for this seat: OFF.** Ignore the ladder above; write what the prompt asks for." ;;
  lite)
    echo "**Ponytail intensity for this seat: lite** — Build what's asked, but name the lazier alternative in one line. User picks." ;;
  ultra)
    echo "**Ponytail intensity for this seat: ultra** — YAGNI extremist. Deletion before addition. Ship the one-liner and challenge the rest of the requirement in the same breath." ;;
  *)
    echo "**Ponytail intensity for this seat: full** — The ladder enforced. Stdlib and native first. Shortest diff, shortest explanation. Default." ;;
esac >> "$CLAUDE_HOME/CLAUDE.md"

if [ -f "$SHELF/INDEX.md" ]; then
  {
    echo
    echo "---"
    echo
    echo "## Shelf index"
    echo
  } >> "$CLAUDE_HOME/CLAUDE.md"
  cat "$SHELF/INDEX.md" >> "$CLAUDE_HOME/CLAUDE.md"
else
  echo "apps-entrypoint: NOTE no $SHELF/INDEX.md — brief composed without a shelf index" >&2
fi

# Copied, not symlinked: a symlink into the read-only shelf breaks turn startup.
if [ -d "$SHELF/ponytail/skills" ]; then
  mkdir -p "$CLAUDE_HOME/skills"
  cp -r "$SHELF/ponytail/skills/." "$CLAUDE_HOME/skills/" 2>/dev/null || \
    echo "apps-entrypoint: WARNING could not install ponytail skills" >&2
fi

if [ "$#" -eq 0 ]; then
  # No argv: the env form keeps the image runnable by hand.
  if [ -n "${APPS_RUNNER_ARGV:-}" ]; then
    # shellcheck disable=SC2086  # deliberate split of a keyboard-set argv
    exec $APPS_RUNNER_ARGV
  fi
  echo "apps-entrypoint: no runner argv (pass it as the container command)" >&2
  exit 64
fi

exec "$@"
