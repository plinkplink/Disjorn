#!/usr/bin/env bash
# 05-gable.sh — Gable residency activation (WP-H9/H10 keyboard step).
# Run as plink with sudo, AFTER: 01-users, 02-podman, 03-network, 04-broker,
# and the spine witness (#custodian seq 66/67 — done 2026-07-20).
# Idempotent-ish: safe to re-run; existing files are left alone where noted.
set -euo pipefail

GABLE_HOME=/home/res-gable
VOL=$GABLE_HOME/resident-home
LIBDIR=/usr/local/lib/disjorn
REPO=/home/plink/Disjorn/Disjorn
CONFIG=/srv/disjorn-resident-config/res-gable

echo "== 1. container image into res-gable's store (wall blocks resident pulls) =="
podman save localhost/disjorn-resident:latest \
  | sudo -u res-gable env XDG_RUNTIME_DIR=/run/user/$(id -u res-gable) podman load

echo "== 2. home volume layout =="
sudo -u res-gable mkdir -p "$VOL"/{bots,memory,logs}
sudo ln -sfn "$VOL/logs" "$GABLE_HOME/logs"   # broker read-own-log target parity

echo "== 3. Gable's own repo (spine) into his volume =="
# Bundle across the wall, same recipe as Claudette's (his repo: ~/bots/fable).
BUNDLE=$(mktemp /tmp/gable-repo-XXXX.bundle)
git -C /home/plink/bots/fable bundle create "$BUNDLE" --all 2>/dev/null
chmod 644 "$BUNDLE"
if sudo -u res-gable test -d "$VOL/bots/fable/.git"; then
  sudo -u res-gable git -C "$VOL/bots/fable" fetch "$BUNDLE" 'refs/heads/*:refs/remotes/host/*'
  sudo -u res-gable git -C "$VOL/bots/fable" merge --ff-only host/main 2>/dev/null || \
  sudo -u res-gable git -C "$VOL/bots/fable" merge --ff-only host/master
else
  sudo -u res-gable git clone "$BUNDLE" "$VOL/bots/fable"
fi
rm -f "$BUNDLE"

echo "== 4. Disjorn worktree (write) — his custodian workbench =="
# Clone from the res-readable mirror; merges to real main flow through the
# broker gate (MERGE-CONTRACT), never from this clone directly.
if ! sudo -u res-gable test -d "$VOL/disjorn/.git"; then
  sudo -u res-gable git clone /srv/disjorn-ro "$VOL/disjorn"
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
