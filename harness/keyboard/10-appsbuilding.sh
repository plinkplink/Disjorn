#!/usr/bin/env bash
# 10-appsbuilding.sh — provision the apps-builder seat (APPS v1 stage 2, §C/§D).
#
# RUN BY: plink, at his keyboard, with sudo:
#
#     sudo bash harness/keyboard/10-appsbuilding.sh            # provision + drift
#     sudo bash harness/keyboard/10-appsbuilding.sh --drift    # drift block only
#     sudo bash harness/keyboard/10-appsbuilding.sh --gate-env # gate.env only
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
#   gate.env /etc/disjorn-apps/gate.env, 0640 root:plink — EXACTLY the four
#            keys the serving gate reads, extracted from the house's
#            server/.env. Wall 5 of the parent spec ("the gate has no database
#            and no house credential") used to be a sentence in a comment while
#            the unit read the house's own .env, which carries SECRET_KEY, the
#            VAPID private key and the DB path (Gable #2546). Now the gate gets
#            one HMAC secret and three strings, and nothing else in that file
#            exists as far as it is concerned.
#   unit     /etc/systemd/system/disjorn-apps-gate.service from
#            deploy/disjorn-apps-gate.service, if that file is in the repo —
#            the stage-3 serving gate (SPECS/2026-09-09-apps-serving-gate.md
#            D1), a second ASGI app run as plink on 127.0.0.1:8402. It is
#            installed HERE and not by deploy/install.sh because the thing it
#            serves is this seat's /srv/apps-www, and the drift block below is
#            the only place on the box that says out loud whether what is
#            running is what is in the repo. GUARDED: the file arrives with the
#            gate hand's branch, and this script must keep working before it
#            does.
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
GATE_UNIT_SRC="$REPO/deploy/disjorn-apps-gate.service"   # written by the gate hand
GATE_UNIT_DST=/etc/systemd/system/disjorn-apps-gate.service
GATE_UNIT_NAME=disjorn-apps-gate.service
# The gate's environment. Overridable ONLY so the test suite can run the
# extraction against a fixture .env as an unprivileged user; on this box every
# one of these is the default (the same discipline 08-gatehouse-repo.sh uses).
SERVER_ENV="${SERVER_ENV:-$REPO/server/.env}"
GATE_ENV="${GATE_ENV:-$ETCDIR/gate.env}"
GATE_ENV_OWNER="${GATE_ENV_OWNER:-root}"
GATE_ENV_GROUP="${GATE_ENV_GROUP:-plink}"   # the uid the gate unit runs as
# THE WHOLE LIST. A fifth key here is a fifth thing the gate could learn about
# the house, and it is a diff someone has to defend.
GATE_ENV_KEYS=(APPS_GATE_SECRET APPS_WWW_ROOT HOUSE_ORIGINS APPS_ORIGIN_BASE)
GATE_SECRET_MIN=32                # server/app/gate.py refuses a shorter one
BROKER_TOML=/etc/disjorn-broker/broker.toml       # read-only, for the drift block

DRIFT_ONLY=0
GATE_ENV_ONLY=0
case "${1:-}" in
  --drift)    DRIFT_ONLY=1 ;;
  --gate-env) GATE_ENV_ONLY=1 ;;
  "")         ;;
  *) echo "usage: 10-appsbuilding.sh [--drift|--gate-env]" >&2; exit 1 ;;
esac

if [[ $EUID -ne 0 ]] && [ "$DRIFT_ONLY" = 0 ]; then
  # The one unprivileged write: `--gate-env` with GATE_ENV pointed somewhere
  # else, which is the test suite running the extraction against a fixture.
  # Everything else here writes /etc, /srv and /usr/local.
  if [ "$GATE_ENV_ONLY" = 0 ] || [ "$GATE_ENV" = "$ETCDIR/gate.env" ]; then
    echo "run with sudo: sudo bash harness/keyboard/10-appsbuilding.sh" >&2
    exit 1
  fi
fi

say() { echo "== $*"; }

# ──────────────────────────────────────────── the gate's own environment ────
# WALL 5 (parent spec): "the gate has no database and no house credential."
# The gate is a second ASGI process of the SAME package in the SAME checkout,
# so until Gable #2546 it simply read the house's server/.env — a file that
# carries SECRET_KEY, the VAPID private key and the path to disjorn.db. It only
# ever wanted four keys. This extracts exactly those four into a file of the
# gate's own, and the unit reads that instead.
#
# NEVER ECHOES A VALUE. Everything below moves whole lines around and measures
# a length; the only thing it prints about the secret is how long it is not.
# Writes through a temp file in the destination directory and one mv, so a
# gate restarting mid-run reads the old file or the new one and never half of
# either. The output is BYTE-DETERMINISTIC (fixed header, fixed key order, no
# timestamp) — that is what lets the drift block below re-extract and diff.
#
#   extract_gate_env <src .env> <dst> [owner] [group]   -> 0 wrote, 1 refused
extract_gate_env() {
  local src="$1" dst="$2" owner="${3:-$GATE_ENV_OWNER}" group="${4:-$GATE_ENV_GROUP}"
  local key line value secret_len tmp
  if [ ! -r "$src" ]; then
    echo "gate.env: cannot read $src — the house's .env is where the four gate keys come from" >&2
    return 1
  fi

  # The secret first, because a missing or short one is a refusal and there is
  # no point writing a file the gate will refuse to boot on. `tail -n1`: dotenv
  # semantics are last-wins, and systemd's EnvironmentFile parser agrees.
  line="$(grep -E "^[[:space:]]*APPS_GATE_SECRET=" "$src" | tail -n1 || true)"
  if [ -z "$line" ]; then
    echo "gate.env: REFUSED — no APPS_GATE_SECRET in $src; the gate verifies house-minted grants and cannot start without the house's signing secret" >&2
    return 1
  fi
  value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"     # a quoted value is the string
  value="${value%\'}"; value="${value#\'}"     # inside the quotes, not them
  secret_len=${#value}
  if [ "$secret_len" -lt "$GATE_SECRET_MIN" ]; then
    echo "gate.env: REFUSED — APPS_GATE_SECRET is $secret_len bytes, under the $GATE_SECRET_MIN the gate requires (server/app/gate.py); generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(48))'" >&2
    return 1
  fi

  mkdir -p "$(dirname "$dst")"
  tmp="$dst.tmp.$$"
  # install, not touch+chmod: the mode and ownership are on the file from the
  # instant it exists, so the window where the secret is 0644 is no window.
  install -o "$owner" -g "$group" -m 0640 /dev/null "$tmp"
  {
    echo "# /etc/disjorn-apps/gate.env — the serving gate's WHOLE environment."
    echo "#"
    echo "# GENERATED by harness/keyboard/10-appsbuilding.sh --gate-env from the"
    echo "# house's server/.env. Do not hand-edit: the drift block re-extracts and"
    echo "# diffs, so an edit here shows up as DRIFT rather than as behaviour."
    echo "# Change the value in server/.env and re-run the script."
    echo "#"
    echo "# Four keys, and four is the point (Gable #2546, parent spec wall 5): the"
    echo "# gate holds one HMAC secret it can verify with and never mint with, plus"
    echo "# three strings. No SECRET_KEY, no VAPID key, no DB_PATH."
    for key in "${GATE_ENV_KEYS[@]}"; do
      line="$(grep -E "^[[:space:]]*${key}=" "$src" | tail -n1 || true)"
      # An absent optional key is left absent: the gate has a default for
      # APPS_WWW_ROOT and APPS_ORIGIN_BASE, and a missing HOUSE_ORIGINS is a
      # refusal the GATE makes, out loud, at boot — not a line this script
      # invents.
      if [ -n "$line" ]; then
        printf '%s\n' "${line#"${line%%[![:space:]]*}"}"   # leading blanks off
      fi
    done
  } > "$tmp"
  mv -f "$tmp" "$dst"
  say "wrote $dst (0640 $owner:$group, ${#GATE_ENV_KEYS[@]} keys, secret $secret_len bytes)"
  return 0
}

if [ "$GATE_ENV_ONLY" = 1 ]; then
  extract_gate_env "$SERVER_ENV" "$GATE_ENV" || exit 1
  exit 0
fi

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
  # The PARENT is shared by every seat (gable/, claudette/, appsbuilding/),
  # each subdirectory carrying its own wall, so the parent is plain 0755
  # root:root and only THIS seat's directory is root:res-appsbuilding 0750.
  # 2026-09-06 this line installed the parent 0750 root:res-appsbuilding
  # and silently locked res-gable and res-claudette out of their own build
  # configs; every resident build then died with "build config dir missing"
  # (Gable's first adapter-drift build, #custodian #2488, 2026-09-09).
  # The seat can traverse its own directory and read the file plink puts
  # there; it cannot write it, and nothing else on the box can read it.
  # THIS SCRIPT NEVER WRITES `env`.
  install -d -o root -g root -m 0755 /srv/disjorn-build-config
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

  # --- the serving gate's environment (stage 3, D1; Gable #2546) -----------
  # BEFORE the unit, because the unit's EnvironmentFile is this file and a
  # restart without it is a gate that does not come up. A refusal here is a
  # NOTE and not an abort, for the same reason the restart below is: a house
  # whose server/.env has no APPS_GATE_SECRET yet still needs its seat, its
  # launcher and its sudoers installed, and the gate says so itself at boot.
  extract_gate_env "$SERVER_ENV" "$GATE_ENV" \
    || say "NOTE $GATE_ENV NOT written — the gate will not start until it is"

  # --- the serving gate's unit (stage 3, D1) -------------------------------
  # IDEMPOTENT and GUARDED. `install` overwrites, daemon-reload makes systemd
  # read it, and `restart` is used rather than `start` so a re-run of this
  # script picks up new gate code instead of quietly leaving the old process
  # running — the stale-deploy shape this whole script exists to catch.
  # `enable` is separate from `restart` so the boot behaviour is set even if
  # the gate itself fails to come up (a missing APPS_GATE_SECRET, which the
  # gate refuses to boot without, is exactly that case and must not abort the
  # provisioning run; so is a gate.env the step above refused to write).
  if [ -f "$GATE_UNIT_SRC" ]; then
    install -o root -g root -m 0644 "$GATE_UNIT_SRC" "$GATE_UNIT_DST"
    systemctl daemon-reload
    systemctl enable "$GATE_UNIT_NAME" >/dev/null 2>&1 \
      || say "NOTE could not enable $GATE_UNIT_NAME — see systemctl status"
    if systemctl restart "$GATE_UNIT_NAME"; then
      say "installed and restarted $GATE_UNIT_NAME"
    else
      say "NOTE $GATE_UNIT_NAME installed but did NOT start — journalctl -u $GATE_UNIT_NAME"
      say "     (the gate refuses to boot without APPS_GATE_SECRET in $GATE_ENV)"
    fi
  else
    say "NOTE no $GATE_UNIT_SRC in the repo yet — skipping the serving gate's unit"
  fi

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
  # Owned by the build user: podman save writes the tar there as that user.
  install -d -m 0755 -o "${SUDO_USER:-plink}" -g "${SUDO_USER:-plink}" /var/cache/disjorn-apps
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

# The serving gate's unit, on the same terms as the four files above — but
# only once the gate hand's file exists in the repo, because "MISSING" for a
# file nobody has written yet is noise, and noise is how a drift block stops
# being read. Reported as its own line either way, and the RUNNING state is
# reported too: a unit file that matches the repo while the service is dead is
# the exact stale-deploy shape this block exists for.
if [ -f "$GATE_UNIT_SRC" ]; then
  if [ ! -e "$GATE_UNIT_DST" ]; then
    echo "  MISSING  $GATE_UNIT_DST"; drift=1
  elif diff -q "$GATE_UNIT_DST" "$GATE_UNIT_SRC" >/dev/null; then
    echo "  same     $GATE_UNIT_DST"
  else
    echo "  DIFFERS  $GATE_UNIT_DST  (vs $GATE_UNIT_SRC)"; drift=1
  fi
  gate_state="$(systemctl is-active "$GATE_UNIT_NAME" 2>/dev/null || true)"
  if [ "$gate_state" = "active" ]; then
    echo "  running  $GATE_UNIT_NAME"
  else
    echo "  NOT UP   $GATE_UNIT_NAME (systemctl is-active: ${gate_state:-unknown})"; drift=1
  fi
else
  echo "  n/a      $GATE_UNIT_NAME — no $GATE_UNIT_SRC in the repo yet"
fi

# The gate's environment, on the SAME terms as the unit: what is installed is
# compared against what a fresh extraction from server/.env would produce. The
# fresh copy goes to a temp file owned by whoever is running this (so `--drift`
# works unprivileged) and `diff -q` is used, so no value is ever printed — the
# answer is "same" or "DIFFERS", which is the whole question.
gate_env_probe="$(mktemp "${TMPDIR:-/tmp}/gate.env.probe.XXXXXX")"
if extract_gate_env "$SERVER_ENV" "$gate_env_probe" "$(id -un)" "$(id -gn)" \
     >/dev/null 2>&1; then
  if [ ! -e "$GATE_ENV" ]; then
    echo "  MISSING  $GATE_ENV"; drift=1
  elif diff -q "$GATE_ENV" "$gate_env_probe" >/dev/null; then
    echo "  same     $GATE_ENV (matches a fresh extraction from $SERVER_ENV)"
  else
    echo "  DIFFERS  $GATE_ENV  (vs a fresh extraction from $SERVER_ENV)"; drift=1
  fi
else
  # The extraction refuses on a missing/short APPS_GATE_SECRET. Say so without
  # re-running it noisily: the sentence it would print is on the provisioning
  # path, and here the fact is enough.
  echo "  REFUSED  $GATE_ENV — $SERVER_ENV has no usable APPS_GATE_SECRET (≥$GATE_SECRET_MIN bytes)"; drift=1
fi
rm -f "$gate_env_probe"

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

# THE MODEL LIVES IN TWO TABLES, which is this project's most expensive
# recurring defect shape: [runner].model in the launcher's config is the truth
# per turn (it becomes the container's --model and the model recorded in
# result.json), while [apps].model in the broker's config rides the `scoped`
# stage event and defaults the ledger line. When they disagree the room says
# one model and another one ran, and nothing anywhere errors. So: read both,
# say both. Read-only, and "unset" is a real answer — a table may legitimately
# leave the key out and take its default.
read_model() {   # <toml> <table> <key>
  python3 - "$1" "$2" "$3" <<'PYMODEL'
import sys, tomllib
path, table, key = sys.argv[1:4]
try:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
except OSError:
    print("unreadable"); raise SystemExit(0)
except Exception:
    print("unparsable"); raise SystemExit(0)
value = (data.get(table) or {}).get(key)
print(value if isinstance(value, str) and value else "unset")
PYMODEL
}
launch_model=$(read_model "$ETCDIR/launch.toml" runner model)
broker_model=$(read_model "$BROKER_TOML" apps model)
if [ "$launch_model" = "$broker_model" ]; then
  echo "  same     model $launch_model (launch.toml [runner] = broker.toml [apps])"
else
  echo "  DIFFERS  model: launch.toml [runner] $launch_model, broker.toml [apps] $broker_model"
  drift=1
fi

# The image's pinned CC version vs the Containerfile's. The label exists for
# exactly this comparison: a rebuild that never happened is invisible otherwise.
want_cc=$(sed -n 's/^ARG CLAUDE_CODE_VERSION=\(.*\)$/\1/p' \
          "$REPO/harness/cc/Containerfile.apps" | head -n1)
# Ask the SEAT's store (that is the image that runs), from / (a nested sudo
# keeps the caller's cwd, which the seat may not be able to enter), with the
# rootless runtime env set, and read the label off .Config where podman
# image inspect keeps it.
seat_uid_d=$(id -u "$SEAT")
have_cc=$(cd / && sudo -u "$SEAT" env XDG_RUNTIME_DIR="/run/user/$seat_uid_d" HOME="/home/$SEAT" \
          podman image inspect --format '{{index .Config.Labels "disjorn.claude-code-version"}}' "$IMAGE" 2>/dev/null || true)
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

Next, at the keyboard: a turn is proved THROUGH THE BROKER VERB now.

Slice (i)'s `keyboard` principal is gone (spec §C wins), so there is no
launcher line left to type — and `sudo -u res-gable ...` is not the
substitute. The prompt has to be a file the SEAT owns inside the seat's own
mapped directory, and the verb is what mints the turn number, the ledger line
and the room's system line; a launcher run around it produces a turn no
session ever hears about.

  1. open the APPS tab in Disjorn and start a build session with the resident;
  2. have the resident hand the prompt off — its `apps_build` tool writes
     ~/apps-prompts/<session>-<turn>.md and calls the broker verb;
  3. watch the turn land, from the outside:
       journalctl -fu disjorn-apps-<session>-<turn>.service
       cat  /srv/apps-turns/<session>/<turn>/result.json
       tail -n1 /var/log/disjorn-broker/apps-ledger.jsonl
       sudo ls -la /srv/apps/<app-id> /srv/apps-www/<app-id>/preview

(A turn that must die early is still `sudo systemctl stop
disjorn-apps-<session>-<turn>.service` — deliberately a keyboard act.)
NEXTEOF
