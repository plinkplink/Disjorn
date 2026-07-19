#!/usr/bin/env bash
# 02-podman.sh — WP-H1 step 2: rootless podman for the residents.
#
# RUN BY: plink, with sudo, AFTER 01-users.sh:   sudo bash 02-podman.sh
# Idempotent: skips installs and subuid entries that already exist.
#
# What it does and why:
#   * installs podman + uidmap (+ slirp4netns as the user-mode network
#     backend) — "rootless headless podman" is the settled container tech
#     (HARNESS-PLAN, seq 31-32);
#   * gives each resident a private /etc/subuid + /etc/subgid range so
#     rootless containers can map uids inside their namespaces;
#   * enables lingering so systemd user services (the residents' container
#     units) can run without anyone logging in as them.
#
# NOTE the boundary promise (Claudette's flag, pinned in HARNESS-PLAN WP-H2):
# podman is CONTAINMENT CONVENIENCE, not the security wall. The egress wall
# is host nftables keyed on these uids — 03-network.sh. Nothing in this
# script is load-bearing for "she can't phone home".
set -euo pipefail

RESIDENTS=(res-claudette res-gable)
# Fixed, documented subordinate ranges (65536 ids each). If either range
# collides with an existing /etc/subuid entry on this box, pick new bases —
# they only need to be unique and unused.
declare -A SUBID_BASE=([res-claudette]=200000 [res-gable]=300000)

# --- packages ---------------------------------------------------------------
for pkg in podman uidmap slirp4netns; do
  if dpkg -s "$pkg" &>/dev/null; then
    echo "== $pkg already installed"
  else
    apt-get install -y "$pkg"
  fi
done

# --- per-resident subordinate ids + lingering -------------------------------
for u in "${RESIDENTS[@]}"; do
  base="${SUBID_BASE[$u]}"
  range_end=$((base + 65535))
  if grep -q "^$u:" /etc/subuid; then
    echo "== $u already has a subuid entry: $(grep "^$u:" /etc/subuid)"
  else
    usermod --add-subuids "$base-$range_end" --add-subgids "$base-$range_end" "$u"
    echo "== $u granted subuid/subgid $base-$range_end"
  fi
  # Lingering: user-level systemd (and thus resident container units) exists
  # even with no login session. Harmless if already enabled.
  loginctl enable-linger "$u"
done

echo
echo "Verify: sudo -u res-gable podman info --format '{{.Host.Security.Rootless}}'"
echo "        (expect: true)   and: grep res- /etc/subuid /etc/subgid"
