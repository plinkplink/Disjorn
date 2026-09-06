#!/bin/sh
# entrypoint.sh — compose the seat's CLAUDE.md, then become the runner.
#
# BAKED INTO THE IMAGE at /usr/local/bin/apps-entrypoint.sh (and shipped in
# /config-image/ alongside the brief it reads). It is the image's ENTRYPOINT,
# so `podman run <image> claude -p ...` reaches it as "$@" and the runner argv
# stays run-apps.sh's business.
#
# WHY THE BRIEF IS ASSEMBLED HERE AND NOT BAKED FLAT
#   Claude Code reads ~/.claude/CLAUDE.md, and $HOME is a per-turn tmpfs — it
#   is empty when this script starts and gone when the turn ends. So the file
#   has to be written at turn start, from image content, every time. Two of its
#   pieces are also not knowable at image-build time: the shelf INDEX.md may or
#   may not be vendored yet, and the ponytail intensity is a runtime knob
#   ([apps].ponytail_mode, arriving as PONYTAIL_DEFAULT_MODE).
#
#   The alternative — putting a CLAUDE.md in /work — was rejected: /work is the
#   user's repo, and the seat must never leave house instructions in the thing
#   the user is going to be handed.
#
# ORDER (the composed file is read top to bottom by the runner):
#   BUILDER-BRIEF.md            the platform contract, §1..§9
#   ponytail AGENTS.md          how much code to write   (if vendored)
#   the intensity line          which ponytail mode this seat runs
#   /shelf/INDEX.md             what is on the shelf     (if vendored)
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

# ── ponytail: instructions only, never a runtime ─────────────────────────────
# The vendored copy is AGENTS.md and skills/*/SKILL.md and nothing else — no
# hooks, no commands, no MCP (spec §F). If the shelf slot is absent (it is
# vendored by the shelf hand), the seat still works; it just writes more code.
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

# The intensity line. The instruction-only install has no runtime mode switch
# (the tracker is one of the excluded hooks), so this line IS the mode: it says
# which of the skill's own intensities applies, and the seat obeys the line.
case "$MODE" in
  off)
    echo "**Ponytail intensity for this seat: OFF.** Ignore the ladder above; write what the prompt asks for." ;;
  lite)
    echo "**Ponytail intensity for this seat: LITE.** Prefer the smaller solution; skip the ladder's deeper questions when the change is obvious." ;;
  ultra)
    echo "**Ponytail intensity for this seat: ULTRA.** Run the whole ladder on every decision, including \"does this need to exist at all\", and justify any file you add." ;;
  *)
    echo "**Ponytail intensity for this seat: FULL.** Run the ladder on every decision; the minimum viable code that satisfies the prompt is the answer." ;;
esac >> "$CLAUDE_HOME/CLAUDE.md"

# ── the shelf index ──────────────────────────────────────────────────────────
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

# ── ponytail's skills ────────────────────────────────────────────────────────
# Copied (not symlinked) into the tmpfs HOME: Claude Code loads skills from
# ~/.claude/skills, the shelf is read-only image content, and a symlink farm
# into a read-only tree is one permission surprise away from a turn that dies
# on startup. Prose only — SKILL.md files, no scripts.
if [ -d "$SHELF/ponytail/skills" ]; then
  mkdir -p "$CLAUDE_HOME/skills"
  cp -r "$SHELF/ponytail/skills/." "$CLAUDE_HOME/skills/" 2>/dev/null || \
    echo "apps-entrypoint: WARNING could not install ponytail skills" >&2
fi

if [ "$#" -eq 0 ]; then
  # No argv: fall back to the env form, so the image is still runnable by hand
  # at the keyboard for a smoke test.
  if [ -n "${APPS_RUNNER_ARGV:-}" ]; then
    # shellcheck disable=SC2086  # deliberate split of a keyboard-set argv
    exec $APPS_RUNNER_ARGV
  fi
  echo "apps-entrypoint: no runner argv (pass it as the container command)" >&2
  exit 64
fi

exec "$@"
