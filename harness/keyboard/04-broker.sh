#!/usr/bin/env bash
# 04-broker.sh — WP-H3: install the disjorn-broker daemon.
#
# RUN BY: plink, with sudo, AFTER 01-users.sh (uids must exist to fill in
# broker.toml) — network (03) order doesn't matter for this step.
# Idempotent: NEVER overwrites an existing config; re-copies unit/sudoers
# only (those are code, config is state).
#
# What lands where:
#   /etc/disjorn-broker/broker.toml     config (template; EDIT the [uids] map!)
#   /etc/disjorn-broker/verbs.toml      THE KILL SWITCHES (all OFF by default)
#   /etc/sudoers.d/90-disjorn-broker    the single privileged escape hatch
#   /etc/systemd/system/disjorn-broker.service
#   /var/lib/disjorn-broker/metrics.json  placeholder until WP-H8/H12 produce it
set -euo pipefail

REPO=/home/plink/Disjorn/Disjorn
SRC=$REPO/harness/broker
ETC=/etc/disjorn-broker

# --- config: copy templates, never clobber ----------------------------------
install -d -m 0750 -o root -g plink "$ETC"
for f in broker.toml verbs.toml; do
  if [ -e "$ETC/$f" ]; then
    echo "== $ETC/$f exists — NOT overwriting (diff against template:)"
    diff -u "$ETC/$f" "$SRC/$f" || true
  else
    # root-owned, plink-group-readable: the broker (runs as plink) reads it;
    # editing goes through sudoedit — the switches stay in plink's drawer,
    # unreachable from any resident container.
    install -m 0640 -o root -g plink "$SRC/$f" "$ETC/$f"
    echo "== installed $ETC/$f"
  fi
done

# --- metrics placeholder (read-metrics serves this until WP-H8/H12) ---------
install -d -m 0755 -o plink -g plink /var/lib/disjorn-broker
if [ ! -e /var/lib/disjorn-broker/metrics.json ]; then
  echo '{}' > /var/lib/disjorn-broker/metrics.json
  chown plink:plink /var/lib/disjorn-broker/metrics.json
fi

# --- sudoers: exactly one line, syntax-checked BEFORE it can lock you out ---
visudo -cf "$REPO/harness/keyboard/90-disjorn-broker.sudoers"
install -m 0440 -o root -g root \
  "$REPO/harness/keyboard/90-disjorn-broker.sudoers" \
  /etc/sudoers.d/90-disjorn-broker
echo "== sudoers line installed (plink -> 'systemctl restart disjorn' only)"

# --- systemd unit -----------------------------------------------------------
install -m 0644 "$SRC/disjorn-broker.service" /etc/systemd/system/disjorn-broker.service
systemctl daemon-reload
systemctl enable --now disjorn-broker

echo
echo "NOW: fill in the real uids -> sudoedit $ETC/broker.toml  ([uids] section,"
echo "     values printed by 01-users.sh), create the broker's bot + API key"
echo "     (server/cli.py) into $ETC/broker-api-key (root:plink 0640),"
echo "     then: sudo systemctl restart disjorn-broker"
echo "Verify: systemctl status disjorn-broker"
echo "        python3 $REPO/harness/keyboard/smoke.py   (expect a clean denial)"
echo "Remember: every verb is OFF. Flip switches in $ETC/verbs.toml when ready."
