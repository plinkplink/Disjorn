#!/usr/bin/env bash
# 05-gable.sh — Gable residency activation (WP-H9/H10 keyboard step).
# Run: sudo ./05-gable.sh  — AFTER: 01-users, 02-podman, 03-network,
# 04-broker, and the spine witness (#custodian seq 66/67 — done 2026-07-20).
# Idempotent-ish: safe to re-run; existing files are left alone where noted.
#
# CONTEXT RULES (each burned us once):
#  - runs as root; per-user work is delegated explicitly (podman stores are
#    per-user: the image lives in PLINK's rootless store, never root's);
#  - cd / first: sudo -u res-* inherits cwd, and a cwd under /home/plink
#    (0700) makes every delegated command die on chdir;
#  - git on another user's repo trips safe.directory — run git AS the owner.
set -euo pipefail
cd /

[ "$(id -u)" -eq 0 ] || { echo "run me with sudo (root)"; exit 1; }
PLINK_UID=$(id -u plink)
GABLE_UID=$(id -u res-gable)
as_plink() { sudo -u plink env XDG_RUNTIME_DIR=/run/user/$PLINK_UID "$@"; }
as_gable() { sudo -u res-gable env XDG_RUNTIME_DIR=/run/user/$GABLE_UID "$@"; }

GABLE_HOME=/home/res-gable
VOL=$GABLE_HOME/resident-home
LIBDIR=/usr/local/lib/disjorn
REPO=/home/plink/Disjorn/Disjorn
CONFIG=/srv/disjorn-resident-config/res-gable

echo "== 1. container image into res-gable's store (wall blocks resident pulls) =="
as_plink podman save localhost/disjorn-resident:latest | as_gable podman load

echo "== 2. home volume layout =="
as_gable mkdir -p "$VOL/bots" "$VOL/memory" "$VOL/logs"
ln -sfn "$VOL/logs" "$GABLE_HOME/logs"   # broker read-own-log target parity

echo "== 3. Gable's own repo (spine) into his volume =="
# Bundle across the wall, same recipe as Claudette's (his repo: ~/bots/fable).
# Bundle created AS PLINK (repo owner — root git would trip safe.directory),
# and the temp file must be plink's too: a root-owned file in sticky /tmp
# can't be replaced by plink's git (lockfile rename EPERM). No stderr
# suppression — a silent set -e death here cost a debugging round.
BUNDLE=$(as_plink mktemp /tmp/gable-repo-XXXX.bundle)
as_plink git -C /home/plink/bots/fable bundle create "$BUNDLE" --all
chmod 644 "$BUNDLE"
if as_gable test -d "$VOL/bots/fable/.git"; then
  as_gable git -C "$VOL/bots/fable" fetch "$BUNDLE" 'refs/heads/*:refs/remotes/host/*'
  as_gable git -C "$VOL/bots/fable" merge --ff-only host/main 2>/dev/null || \
  as_gable git -C "$VOL/bots/fable" merge --ff-only host/master
else
  as_gable git clone "$BUNDLE" "$VOL/bots/fable"
fi
rm -f "$BUNDLE"

echo "== 4. Disjorn worktree (write) — his custodian workbench =="
# Clone from the res-readable mirror; merges to real main flow through the
# broker gate (MERGE-CONTRACT), never from this clone directly. The mirror
# is plink-owned, so res-gable's git needs it marked safe (durable — his
# workbench keeps fetching from it). BOTH path forms: the local transport's
# upload-pack resolves the repo to its .git dir and checks THAT string, so
# the bare parent-path entry alone still trips the wall (run 3's lesson).
for safe in /srv/disjorn-ro /srv/disjorn-ro/.git; do
  as_gable git config --global --get-all safe.directory | grep -qx "$safe" || \
    as_gable git config --global --add safe.directory "$safe"
done
if ! as_gable test -d "$VOL/disjorn/.git"; then
  as_gable git clone /srv/disjorn-ro "$VOL/disjorn"
fi

echo "== 5. residency package + venv (host-side daemon deps) =="
# Built by ROOT (plink uids are unwalled; res-gable's egress can't reach PyPI
# — intended). res-gable only ever READS these.
install -d "$LIBDIR"
rm -rf "$LIBDIR/residency" "$LIBDIR/disjorn_sdk_pkg"
cp -r "$REPO/harness/residency" "$LIBDIR/residency"
cp -r "$REPO/sdk" "$LIBDIR/disjorn_sdk_pkg"
if [ ! -d "$LIBDIR/residency-venv" ]; then
  python3 -m venv "$LIBDIR/residency-venv"
fi
"$LIBDIR/residency-venv/bin/pip" -q install httpx websockets
"$LIBDIR/residency-venv/bin/pip" -q install -e "$LIBDIR/disjorn_sdk_pkg"
chmod -R a+rX "$LIBDIR/residency" "$LIBDIR/disjorn_sdk_pkg" "$LIBDIR/residency-venv"

echo "== 6. summon service =="
install -D -m 0644 "$REPO/harness/residency/gable-summon.service" \
  "$GABLE_HOME/.config/systemd/user/gable-summon.service"
chown -R res-gable: "$GABLE_HOME/.config"
systemctl --user -M res-gable@ daemon-reload

echo "== 7. broker: log_path for read-own-log (add once, sudoedit) =="
grep -q "res-gable" /etc/disjorn-broker/broker.toml && \
  grep -q "log_path.*res-gable" /etc/disjorn-broker/broker.toml || cat <<'NOTE'
  MANUAL: add to /etc/disjorn-broker/broker.toml:
    [residents.res-gable]
    log_path = "/home/res-gable/logs/summon.log"
  then: sudo systemctl restart disjorn-broker
NOTE

echo "== 8. ACL: plink read on his logs (broker serves read-own-log as plink) =="
setfacl -m u:plink:x "$GABLE_HOME" "$VOL" || true
setfacl -m u:plink:rx "$VOL/logs" || true

cat <<'DONE'
== REMAINING, deliberately manual (plink's levers) ==
  a. ANTHROPIC_API_KEY into /srv/disjorn-resident-config/res-gable/env
  b. Flip res-gable verbs in /etc/disjorn-broker/verbs.toml — one at a time,
     read-or-propose first (read-own-log, read-metrics, file-proposal,
     query-own-audit), same ramp as hers.
  c. Start the residency:  sudo systemctl --user -M res-gable@ enable --now gable-summon
  d. First summon: @Gable in a channel; session summary lands in #custodian.
DONE
