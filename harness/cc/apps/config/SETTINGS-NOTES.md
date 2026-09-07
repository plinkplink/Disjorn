# What each line of `settings.json` is for

JSON has no comments and this file is policy, so the reasons live here. The
file is baked into the image at `/config-image/settings.json` and symlinked
from `/etc/claude-code/managed-settings.json`, which Claude Code treats as the
highest-precedence layer: the turn's own `~/.claude/settings.json` (in the
per-turn tmpfs HOME) cannot override it, and the turn cannot edit
`/config-image` — it is root-owned image content and the seat runs as uid 1000.

**This file is hygiene, not the wall.** The walls are elsewhere and they are
the ones that hold: the userns + the read-only mounts (nothing outside `/work`
is writable at all), and the WP-H2 host nftables rules keyed on the
`res-appsbuilding` uid (the model API is reachable and nothing else is). A
`deny` here stops the model's *tool* from doing something; it does not stop a
process. Read every line below as "make the wrong thing hard to do by
accident", never as "make it impossible".

## `permissions.defaultMode: "acceptEdits"`
The seat is non-interactive: there is no human to approve an edit and a prompt
that waits for one is a turn that burns its whole clock and halts on timeout.

## `additionalDirectories: ["/shelf"]`
Claude Code confines file tools to the working directory (`/work`) by default.
The shelf is outside it and the whole point is to copy from it, so it is added
explicitly — read-only both by this file's `deny` and by the image (`chmod -R
a-w`).

## `allow`
- `Read/Edit/Write(//work/**)` — the app repo. The only writable path that
  persists, and the only place output belongs.
- `Read(//shelf/**)` — the vendored assets.
- `Glob`, `Grep` — reading the repo before writing it is rule 1 of the brief;
  the seat must not need permission to look.
- `Bash(ls|cat|head|tail|mkdir|cp|mv|rm:*)` — the file moves the brief tells it
  to make ("copy what you use into `/work/vendor/`"). Scoped by prefix, which
  is what Claude Code's Bash matcher can express.
- `Bash(node:*)` — a static app has no build step, but a one-off `node -e` to
  check a JS file parses is honest work and needs no network.

Deliberately NOT allowed: `git`. The harvest owns this repo's history — it
commits `turn N` itself, from outside the container — and a turn that made its
own commits would fight it for what a turn means. `git` is not denied either:
it is simply absent from `allow`, so a use of it surfaces as a prompt rather
than a silent success.

## `deny`
- `WebFetch`, `WebSearch` — no egress is the seat's contract (spec §A). These
  are the two tools that would try, and they are denied outright rather than
  left to fail against nftables, so the refusal is legible instead of looking
  like a network fault.
- `Bash(curl|wget|nc|ncat|socat|ssh|scp:*)` — the same wall at the shell.
- `Bash(npm|npx|pip:*)` — no package install. The app is static files; the
  shelf is the dependency story, and a turn that could install could also
  fetch.
- `Bash(git push:*)` — there is no remote and there must never be one; denied
  by name so it reads as a decision rather than an omission.
- `Bash(sudo|su:*)` — nothing to escalate to, and an attempt should be loud.
- `Read(//config/**)` and `Read(//config-image/**)` — `/config` is the
  credential drop directory. `/config/env` is additionally masked with
  `/dev/null` by run-apps.sh, and the credential still lives in the session's
  own environment, so this line is the hygiene it is labelled as.
- `Edit/Write(//config/**)`, `Edit/Write(//shelf/**)` — both are read-only
  mounts/image content; denying the tool turns a confusing EROFS into a plain
  refusal.

## What is NOT here, and why
No hooks. The resident image's `config-template` wires three (session-start,
pre-tool-use, action-counter) because a resident has a house to answer to; this
seat has no broker socket, no house memory and no action log, so every one of
them would be a script with nothing to say. The brief in `~/.claude/CLAUDE.md`
is the whole instruction surface.
