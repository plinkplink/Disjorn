#!/usr/bin/env bash
# 10-appsbuilding.sh — provision the apps-builder seat (APPS v1 stage 2, §C/§D).
#
# RUN BY: plink, at his keyboard, with sudo:
#
#     sudo bash harness/keyboard/10-appsbuilding.sh          # provision + drift
#     sudo bash harness/keyboard/10-appsbuilding.sh --drift  # drift block only
#
# IDEMPOTENT. It never deletes anything, never overwrites an existing
# /etc/disjorn-apps/launch.toml, and NEVER writes the credential — plink writes
# that file himself; this script only creates the directory and a README saying
# exactly what goes in it.
#
# It is 01-users.sh + 02-podman.sh + 07-resident-image.sh for one new seat,
# plus the drift block that the parent spec asked for in as many words ("learn
# from the build-seat provisioning gaps"): the stale-deploy family is this
# project's most reliable way to lose an evening, and it has fired at least
# four times — an uninstalled fix, a stale container serving old code, a
# wrapper never deployed while the config already pointed at it, a resident
# reporting a missing tool that was genuinely missing. So the last thing this
# script does is DIFF what is installed against what is in the repo, and print
# the answer whether or not it is good news.
#
# WHAT IT PROVISIONS
#   user     res-appsbuilding — system account, own subuid/subgid range,
#            lingering on for rootless podman, in NO privileged group.
#   dirs     /srv/apps            0750 res-appsbuilding  app repos
#            /srv/apps-www        0755                   preview roots
#            /srv/apps-turns      0755                   per-turn result dirs
#            /srv/apps-quarantine 0700                   never mounted anywhere
#            /srv/disjorn-build-config/appsbuilding 0750 root:res-appsbuilding
#   config   /etc/disjorn-apps/launch.toml from the repo's example, if absent.
#   code     /usr/local/lib/disjorn/{disjorn-apps-launch,run-apps.sh,
#            apps_harvest.py}, root:root 0755.
#   sudoers  /etc/sudoers.d/92-disjorn-apps, 0440, visudo -c'd BEFORE install.
#   image    localhost/disjorn-apps-builder:latest, built in plink's store (the
#            only one with registry egress) and loaded into the seat's.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SEAT=res-appsbuilding
SUBID_BASE=400000                 # 200000 claudette, 300000 gable (02-podman.sh)
IMAGE=localhost/disjorn-apps-builder:latest
TAR=/var/cache/disjorn-apps/disjorn-apps-builder.tar   # not /tmp or /var/tmp: see the STAGE note
LIBDIR=/usr/local/lib/disjorn
ETCDIR=/etc/disjorn-apps
CONFIG_DIR=/srv/disjorn-build-config/appsbuilding
SUDOERS_SRC="$REPO/harness/keyboard/92-disjorn-apps.sudoers"
SUDOERS_DST=/etc/sudoers.d/92-disjorn-apps

DRIFT_ONLY=0
[ "${1:-}" = "--drift" ] && DRIFT_ONLY=1

if [[ $EUID -ne 0 ]] && [ "$DRIFT_ONLY" = 0 ]; then
  echo "run with sudo: sudo bash harness/keyboard/10-appsbuilding.sh" >&2
  exit 1
fi

say() { echo "== $*"; }

# ─────────────────────────────────────────────────────────── provisioning ────
if [ "$DRIFT_ONLY" = 0 ]; then

  # --- host packages -------------------------------------------------------
  # rsync is the harvest's preview copy (§E) and is NOT installed on a stock
  # Debian trixie box — this is the one new host dependency the slice adds.
  # podman/uidmap/slirp4netns are already here from 02-podman.sh; asserted
  # rather than assumed, because a missing one fails at the first turn.
  for pkg in podman uidmap slirp4netns rsync git; do
    if dpkg -s "$pkg" &>/dev/null; then
      say "$pkg already installed"
    else
      apt-get install -y "$pkg"
    fi
  done

  # --- the seat account ----------------------------------------------------
  if id "$SEAT" &>/dev/null; then
    say "$SEAT already exists (uid $(id -u "$SEAT")) — leaving it alone"
  else
    # --system: a service identity, not a person. nologin: nobody su's into
    # it; podman is reached through the transient unit's uid, which needs no
    # login shell.
    useradd --system --user-group \
            --create-home --home-dir "/home/$SEAT" \
            --shell /usr/sbin/nologin \
            --comment "Disjorn apps builder seat" \
            "$SEAT"
    say "created $SEAT (uid $(id -u "$SEAT"))"
  fi
  chmod 0700 "/home/$SEAT"
  # Belt and braces, exactly as 01-users.sh: assert the account is in no
  # privileged group. A seat that could sudo is not a seat.
  for g in sudo adm root wheel; do
    if id -nG "$SEAT" | tr ' ' '\n' | grep -qx "$g"; then
      echo "!! $SEAT is in group $g — removing"
      gpasswd -d "$SEAT" "$g"
    fi
  done

  # --- rootless podman -----------------------------------------------------
  if grep -q "^$SEAT:" /etc/subuid; then
    say "$SEAT already has a subuid entry: $(grep "^$SEAT:" /etc/subuid)"
  else
    usermod --add-subuids "$SUBID_BASE-$((SUBID_BASE + 65535))" \
            --add-subgids "$SUBID_BASE-$((SUBID_BASE + 65535))" "$SEAT"
    say "$SEAT granted subuid/subgid $SUBID_BASE-$((SUBID_BASE + 65535))"
  fi
  # Lingering: /run/user/<uid> must exist for rootless podman inside a
  # transient unit, and no one ever logs in as this account.
  loginctl enable-linger "$SEAT"

  # --- the /srv tree (spec §C) --------------------------------------------
  # 0750 on the repos: an app's SOURCE is the seat's own. The preview root is
  # the world-readable COPY, and it is a copy for exactly this reason — the
  # stage-3 gate is a house process and reads it without being this user.
  install -d -o "$SEAT" -g "$SEAT" -m 0750 /srv/apps
  install -d -o "$SEAT" -g "$SEAT" -m 0755 /srv/apps-www
  install -d -o "$SEAT" -g "$SEAT" -m 0755 /srv/apps-turns
  # 0700 and NEVER MOUNTED into any seat: the quarantine holds files a turn
  # tried to publish a credential in, and the brief tells the next turn to read
  # /work first — which would read the injection straight back in (#2293).
  install -d -o "$SEAT" -g "$SEAT" -m 0700 /srv/apps-quarantine
  say "/srv/apps, /srv/apps-www, /srv/apps-turns, /srv/apps-quarantine ready"

  # --- the credential drop point (spec §D) --------------------------------
  # root:res-appsbuilding 0750 — the seat can traverse it and read the file
  # plink puts there; it cannot write it, and nothing else on the box can read
  # it. THIS SCRIPT NEVER WRITES `env`.
  install -d -o root -g "$SEAT" -m 0750 /srv/disjorn-build-config
  install -d -o root -g "$SEAT" -m 0750 "$CONFIG_DIR"
  cat > "$CONFIG_DIR/README" <<'READMEEOF'
The apps-builder seat's credential goes in this directory, in a file named

    env

that plink writes BY HAND. This provisioning script never writes it and never
reads it.

    sudo install -m 0640 -o root -g res-appsbuilding /dev/null \
         /srv/disjorn-build-config/appsbuilding/env
    sudo sh -c 'echo "ANTHROPIC_API_KEY=sk-ant-..." \
         > /srv/disjorn-build-config/appsbuilding/env'

Exactly one line, exactly that variable name, no quotes around the value
(podman's env-file parser takes everything after the first "=" literally, and
a quote would become part of the key).

WHICH KEY: a DEDICATED, metered Anthropic API key for this seat and nothing
else, so app-build spend is its own line on the bill and can be revoked without
touching a resident. That — dedicated, metered, rotatable — is the actual
containment (spec §D). The harvest's secret scan, which refuses to commit or
publish a turn whose output contains this value in raw, base64 or hex form, is
defence in depth on top of it.

NO OAuth token here. The resident seats route to plink's Max subscription and
refuse a bare API key; this seat is the deliberate opposite and accepts only
ANTHROPIC_API_KEY.

Mode 0640 root:res-appsbuilding. run-apps.sh passes the value into the
container by NAME (never in argv) and masks this directory's `env` with
/dev/null inside, so the turn cannot read the file back out.
READMEEOF
  chmod 0644 "$CONFIG_DIR/README"
  if [ -f "$CONFIG_DIR/env" ]; then
    say "credential present: $CONFIG_DIR/env ($(stat -c '%U:%G %a' "$CONFIG_DIR/env"))"
  else
    say "NOTE credential ABSENT: $CONFIG_DIR/env — see $CONFIG_DIR/README (plink writes it)"
  fi

  # --- the launcher's config table ----------------------------------------
  install -d -o root -g root -m 0755 "$ETCDIR"
  if [ -f "$ETCDIR/launch.toml" ]; then
    say "$ETCDIR/launch.toml already exists — NOT overwriting (drift block below)"
  else
    install -o root -g root -m 0644 \
        "$REPO/harness/cc/apps/launch.toml.example" "$ETCDIR/launch.toml"
    say "installed $ETCDIR/launch.toml from the repo example"
  fi

  # --- the code ------------------------------------------------------------
  install -d -o root -g root -m 0755 "$LIBDIR"
  install -o root -g root -m 0755 \
      "$REPO/harness/cc/apps/disjorn-apps-launch" "$LIBDIR/disjorn-apps-launch"
  install -o root -g root -m 0755 \
      "$REPO/harness/cc/apps/run-apps.sh" "$LIBDIR/run-apps.sh"
  install -o root -g root -m 0755 \
      "$REPO/harness/cc/apps/apps_harvest.py" "$LIBDIR/apps_harvest.py"
  say "installed launcher, wrapper and harvest into $LIBDIR"

  # --- sudoers -------------------------------------------------------------
  # visudo -c BEFORE install, always. A syntax error in /etc/sudoers.d locks
  # sudo out of the whole box, and this is the one file here that could.
  visudo -cf "$SUDOERS_SRC"
  install -o root -g root -m 0440 "$SUDOERS_SRC" "$SUDOERS_DST"
  say "installed $SUDOERS_DST (0440)"

  # --- the image -----------------------------------------------------------
  # Built in PLINK's store (podman stores are per-user, and the WP-H2 egress
  # wall blocks registry pulls from res-* uids, deliberately), then saved and
  # loaded into the seat's store. Image updates are a plink action, permanently.
  #
  # The context is STAGED and small: the shelf, the seat's config, and the
  # Containerfile. The repo root must never be a build context (871MB, and it
  # contains the production database).
  # NOT under /tmp or /var/tmp: a rootless podman joins its long-lived pause
  # process's mount namespace, and a caller with a private tmp (a sandboxed
  # shell, a PrivateTmp unit) hands it a path that does not exist there.
  install -d -m 0755 /var/cache/disjorn-apps
  STAGE="$(mktemp -d /var/cache/disjorn-apps/ctx.XXXXXX)"
  trap 'rm -rf "$STAGE"; rm -f "$TAR"' EXIT
  mkdir -p "$STAGE/shelf" "$STAGE/apps"
  if [ -d "$REPO/harness/cc/shelf" ]; then
    cp -a "$REPO/harness/cc/shelf/." "$STAGE/shelf/"
  else
    say "NOTE no harness/cc/shelf yet — building with an EMPTY /shelf (the brief and entrypoint degrade to 'no shelf index')"
  fi
  cp -a "$REPO/harness/cc/apps/config" "$STAGE/apps/config"
  cp "$REPO/harness/cc/Containerfile.apps" "$STAGE/Containerfile.apps"
  rm -rf "$STAGE"/**/__pycache__ "$STAGE"/__pycache__

  BUILD_AS="${SUDO_USER:-plink}"
  say "build as $BUILD_AS (the store with registry egress)"
  chmod -R a+rX "$STAGE"
  # cd to the staged context first: a nested sudo keeps the caller's cwd,
  # and this script is often run from a directory the build user cannot
  # enter (a scratch worktree), which sudo refuses with "cannot chdir".
  # Rootless podman under a nested sudo needs its runtime dir and HOME set
  # explicitly (the same env the load step below already passes), or its
  # re-exec dies with "cannot chdir".
  build_uid=$(id -u "$BUILD_AS")
  as_builder() { (cd "$STAGE" && sudo -u "$BUILD_AS" env XDG_RUNTIME_DIR="/run/user/$build_uid" HOME="/home/$BUILD_AS" "$@"); }
  as_builder podman build -t "$IMAGE" -f "$STAGE/Containerfile.apps" "$STAGE"
  NEW_ID=$(as_builder podman inspect --format '{{.Id}}' "$IMAGE")
  say "built ${NEW_ID:0:12}"

  as_builder podman save -o "$TAR" "$IMAGE"
  chmod 0644 "$TAR"
  seat_uid=$(id -u "$SEAT")
  (cd / && sudo -u "$SEAT" env XDG_RUNTIME_DIR="/run/user/$seat_uid" \
       HOME="/home/$SEAT" podman load -i "$TAR")
  say "$SEAT: image loaded"
fi

# ───────────────────────────────────────────────────────────── drift block ───
# Everything below only reads. Run it any time: `sudo bash 10-appsbuilding.sh
# --drift`. It answers one question — is what is RUNNING what is in the repo —
# and it answers it out loud either way.
echo
echo "== DRIFT: installed vs $REPO =="
drift=0
for pair in \
    "$LIBDIR/disjorn-apps-launch:$REPO/harness/cc/apps/disjorn-apps-launch" \
    "$LIBDIR/run-apps.sh:$REPO/harness/cc/apps/run-apps.sh" \
    "$LIBDIR/apps_harvest.py:$REPO/harness/cc/apps/apps_harvest.py" \
    "$SUDOERS_DST:$SUDOERS_SRC" ; do
  installed="${pair%%:*}"; source="${pair#*:}"
  if [ ! -e "$installed" ]; then
    echo "  MISSING  $installed"; drift=1
  elif diff -q "$installed" "$source" >/dev/null; then
    echo "  same     $installed"
  else
    echo "  DIFFERS  $installed  (vs $source)"; drift=1
  fi
done

# launch.toml is deliberately NOT overwritten by this script, so a difference
# here is expected the moment plink edits a prompt dir. Reported, not judged.
if [ -e "$ETCDIR/launch.toml" ]; then
  if diff -q "$ETCDIR/launch.toml" "$REPO/harness/cc/apps/launch.toml.example" >/dev/null; then
    echo "  same     $ETCDIR/launch.toml (still the shipped example)"
  else
    echo "  edited   $ETCDIR/launch.toml (expected once plink tunes it; diff it by hand)"
  fi
else
  echo "  MISSING  $ETCDIR/launch.toml"; drift=1
fi

# The image's pinned CC version vs the Containerfile's. The label exists for
# exactly this comparison: a rebuild that never happened is invisible otherwise.
want_cc=$(sed -n 's/^ARG CLAUDE_CODE_VERSION=\(.*\)$/\1/p' \
          "$REPO/harness/cc/Containerfile.apps" | head -n1)
have_cc=$(sudo -u "${SUDO_USER:-plink}" podman inspect \
          --format '{{index .Labels "disjorn.claude-code-version"}}' "$IMAGE" 2>/dev/null || true)
if [ -z "$have_cc" ]; then
  echo "  MISSING  image $IMAGE (or it carries no disjorn.claude-code-version label)"; drift=1
elif [ "$have_cc" = "$want_cc" ]; then
  echo "  same     image claude-code $have_cc"
else
  echo "  DIFFERS  image claude-code $have_cc, Containerfile.apps says $want_cc — REBUILD"; drift=1
fi

# The resident image's pin must equal this one's; the test suite asserts the
# same thing against the two files, this asserts it against what is installed.
res_cc=$(sed -n 's/^ARG CLAUDE_CODE_VERSION=\(.*\)$/\1/p' \
         "$REPO/harness/cc/Containerfile" | head -n1)
if [ "$res_cc" = "$want_cc" ]; then
  echo "  same     resident/apps Containerfile pins both claude-code $want_cc"
else
  echo "  DIFFERS  resident Containerfile pins $res_cc, apps pins $want_cc"; drift=1
fi

if [ -f "$CONFIG_DIR/env" ]; then
  echo "  present  $CONFIG_DIR/env ($(stat -c '%U:%G %a' "$CONFIG_DIR/env"))"
else
  echo "  ABSENT   $CONFIG_DIR/env — plink writes it; see $CONFIG_DIR/README"; drift=1
fi

echo
if [ "$drift" = 0 ]; then
  echo "no drift."
else
  echo "DRIFT ABOVE. Believe what you read, then fix it — a stale deploy looks"
  echo "exactly like a code bug and costs an evening every time."
fi

cat <<'NEXTEOF'

Next, at the keyboard, to prove the seat (slice (i) has no broker verb):

  mkdir -p ~/apps-prompts && echo "Build a one-page tip calculator." > ~/apps-prompts/1-1.md
  sudo /usr/local/lib/disjorn/disjorn-apps-launch run keyboard 1 1 aaaaaaaaaaaa \
       /home/plink/apps-prompts/1-1.md
  cat /srv/apps-turns/1/1/result.json
  sudo ls -la /srv/apps/aaaaaaaaaaaa /srv/apps-www/aaaaaaaaaaaa/preview

(The app id is any 12 lowercase base32 characters; stage 1 mints real ones.)
NEXTEOF
