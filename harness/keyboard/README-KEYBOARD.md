# Keyboard session runbook — WP-H1/H2/H3 substrate install

One sitting, plink at the keyboard, sudo in hand. Nothing in this directory
runs itself; nothing here was executed with privileges during authoring —
**read each script before running it**. All four are idempotent: re-running
after a partial failure is safe and never clobbers existing config.

Total estimated time: **30–45 minutes** including verification.

| step | script | what it does | needs |
|------|--------|--------------|-------|
| 1 | `01-users.sh` | creates `res-claudette` + `res-gable` (system users, 0700 homes, nologin, no sudo) | — |
| 2 | `02-podman.sh` | installs podman/uidmap/slirp4netns, subuid/subgid ranges, lingering | step 1 |
| 3 | `03-network.sh` | nftables egress wall keyed on the two uids + anthropic-IP refresh timer | step 1 |
| 4 | `04-broker.sh` | installs broker config/sudoers/unit, starts disjorn-broker | step 1 |

Steps 3 and 4 are independent of each other; keep the order anyway for a
predictable transcript.

---

## Step 1 — users

```sh
sudo bash 01-users.sh
```

Verify:

```sh
id res-claudette && id res-gable      # exist, no sudo/adm groups listed
ls -ld /home/res-*                    # drwx------ each, owned by that resident
sudo -l -U res-claudette              # "not allowed to run sudo"
```

The script prints the two uids in `[uids]`-ready form — **copy that output**,
step 4 needs it.

Rollback: `sudo userdel -r res-claudette res-gable` (destroys their homes —
fine before anything lives there, unthinkable after).

## Step 2 — podman

```sh
sudo bash 02-podman.sh
```

Verify:

```sh
grep res- /etc/subuid /etc/subgid     # one range per resident
# cd / first: sudo keeps your cwd, and resident users can't traverse /home/plink
# (that's the 0700 walls working). env line makes podman's runtime dir explicit.
(cd / && sudo -u res-gable env XDG_RUNTIME_DIR=/run/user/$(id -u res-gable) \
  podman info --format '{{.Host.Security.Rootless}}')   # true
sudo -u res-gable podman run --rm docker.io/library/alpine echo hi     # hi
```

(The alpine pull happens BEFORE step 3's wall exists; after step 3 image
pulls need a plink-side pull + copy, which is the intended shape.)

Rollback: `sudo loginctl disable-linger res-…`; remove the `res-*:` lines
from /etc/subuid + /etc/subgid; `apt remove podman` if it wasn't there before.

## Step 3 — network wall

```sh
sudo bash 03-network.sh
```

The script echoes the resolver IP it will allow DNS to — **sanity-check that
line** before moving on (127.0.0.53 on systemd-resolved boxes).

Verify:

```sh
sudo nft list table inet disjorn_residents      # sets populated, chain present
sudo nft list set inet disjorn_residents anthropic_v4   # non-empty
# The wall in action (as a resident): allowed / blocked / blocked
sudo -u res-gable curl -sm5 https://api.anthropic.com/  -o /dev/null -w '%{http_code}\n'  # 4xx = reached it
sudo -u res-gable curl -sm5 https://example.com          # hangs -> timeout
sudo journalctl -k --since -5min | grep disjorn-wall-drop  # the drop, logged
# plink himself is untouched:
curl -sm5 https://example.com -o /dev/null -w '%{http_code}\n'          # 200
```

Rollback: `sudo nft delete table inet disjorn_residents`; remove the include
line from /etc/nftables.conf; `sudo systemctl disable --now
disjorn-anthropic-refresh.timer`.

## Step 4 — broker

```sh
sudo bash 04-broker.sh
```

Then the two manual edits it prompts for:

1. `sudoedit /etc/disjorn-broker/broker.toml` — fill `[uids]` with the values
   step 1 printed.
2. Create the broker's own bot + API key on the Disjorn server (server
   `cli.py`), key into `/etc/disjorn-broker/broker-api-key` (root:plink
   0640), and set `custodian_channel_id`.
3. `sudo systemctl restart disjorn-broker`

Verify:

```sh
systemctl status disjorn-broker                      # active (running), User=plink
ls -l /run/disjorn-broker/broker.sock                # socket exists
python3 smoke.py                                     # as plink: unknown-caller — GOOD
sudo -u res-claudette python3 smoke.py               # verb-disabled — GOOD (all OFF)
sudo tail -2 /var/log/disjorn-broker/audit.jsonl     # both attempts audited
sudo -u plink sudo -n systemctl restart disjorn      # the one sudoers grant works
curl -s http://127.0.0.1:8399/healthz                # {"ok":true} — disjorn back up
```

Both smoke denials are the healthy state: auth, kill switches and audit all
demonstrably in the loop, with zero verbs granted.

Rollback: `sudo systemctl disable --now disjorn-broker`; `sudo rm
/etc/sudoers.d/90-disjorn-broker /etc/systemd/system/disjorn-broker.service`;
`/etc/disjorn-broker/` can stay (it's inert without the daemon).

---

## Waking a resident (SPECS/2026-08-25-agentic-residents.md)

```sh
python3 wake.py res-gable "read this morning's digest and open a card for
what looks wrong"
```

**As plink, from the keyboard, and never with `sudo -u`.** The wake is a broker
verb whose caller is the uid the kernel reports at the socket, so running it as
anyone else is refused and audited — that refusal is the design, not a
misconfiguration. It prints a wake id; the seat's own runner starts the session,
and the seat — not this command — posts the result in #custodian.

Arming it is four plink-owned edits, none of which a resident can reach:

1. `[uids]` gains plink's own uid (`"1000" = "plink"`);
2. `[wake]` in `broker.toml` — callers, wakeable seats, the spool dir, the
   session cap. The broker REFUSES TO START if the spool is writable by any
   resident, if a caller has no uid, or if a caller is a seat;
3. `sudoedit /etc/disjorn-broker/verbs.toml` — `[plink] "wake" = true`;
4. the seat's own `gable-wake.service` (harness/residency/), which is what
   actually runs the session and posts.

Stopping any one of the four stops the lane. Nothing self-wakes: there is no
cron, no chat surface, and no verb a resident can press to reach this.

## After the session

- Flip individual switches with `sudoedit /etc/disjorn-broker/verbs.toml` —
  takes effect on the next call, no restart. Everything ships OFF.
- The audit trail is `/var/log/disjorn-broker/audit.jsonl` (one JSON line per
  call, denials included).
- Protocol for callers: `harness/broker/PROTOCOL.md`.
- Container build-out, resident CC profiles, classifier etc. are later work
  packages (WP-H4+) and need no further sudo except where marked [keyboard].
