#!/usr/bin/env bash
# 01-users.sh — WP-H1 step 1: create the resident unix users.
#
# RUN BY: plink, at his keyboard, with sudo:   sudo bash 01-users.sh
# READ IT FIRST. Idempotent: safe to re-run; it never deletes anything.
#
# Creates res-claudette and res-gable as system accounts with:
#   * a real home dir (their container volumes / repos live under it),
#   * mode 0700 homes — the residents' homes are MUTUALLY UNREADABLE
#     (AGENTHOOD: "separate credentials, ... mutually unreadable homes"),
#   * /usr/sbin/nologin as shell — nobody ssh/su's into these accounts;
#     containers and broker calls don't need a login shell (podman is invoked
#     via `sudo -u res-X podman ...` or a systemd user unit, neither of which
#     uses the account's shell),
#   * NO sudo group membership, ever — verified below.
set -euo pipefail

RESIDENTS=(res-claudette res-gable)

for u in "${RESIDENTS[@]}"; do
  if id "$u" &>/dev/null; then
    echo "== $u already exists (uid $(id -u "$u")) — leaving it alone"
  else
    # --system: uid from the system range; these are service identities,
    # not people. --user-group: private primary group of the same name.
    useradd --system --user-group \
            --create-home --home-dir "/home/$u" \
            --shell /usr/sbin/nologin \
            --comment "Disjorn resident ($u)" \
            "$u"
    echo "== created $u (uid $(id -u "$u"))"
  fi

  # 0700 home: the other resident (and every non-root user) reads nothing.
  chmod 0700 "/home/$u"

  # Belt-and-braces: assert the account is in no privileged group.
  for g in sudo adm root wheel; do
    if id -nG "$u" | tr ' ' '\n' | grep -qx "$g"; then
      echo "!! $u is in group $g — removing"
      gpasswd -d "$u" "$g"
    fi
  done
done

echo
echo "Done. Record these uids in /etc/disjorn-broker/broker.toml [uids]:"
for u in "${RESIDENTS[@]}"; do
  echo "  \"$(id -u "$u")\" = \"$u\""
done
echo "Verify: id res-claudette; id res-gable; ls -ld /home/res-*"
