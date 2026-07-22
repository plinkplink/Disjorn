#!/usr/bin/env bash
# test_container.sh — WP-H5 image + wrapper smoke test.
#
# Builds the resident image, boots scratch containers through
# run-resident.sh with a FAKE broker socket (tests/fake_broker.py on the
# host) and scratch /config + home mounts, and verifies the promises the
# container is supposed to keep. Rootless podman as the CURRENT user
# (keep-id maps us to 'resident' inside — same mechanism the res-* users
# get). No real API key anywhere; no CC session is started against the API.
#
# Usage: bash tests/test_container.sh [--no-build]
set -uo pipefail

CC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="localhost/disjorn-resident:test"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/wp-h5-smoke.XXXXXX")"
FAKE_PID=""
FAILS=0

say()  { printf '%s\n' "$*"; }
pass() { say "PASS: $*"; }
fail() { say "FAIL: $*"; FAILS=$((FAILS+1)); }

cleanup() {
  [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null
  podman rm -f resident-cc-smoketest &>/dev/null
  # The scratch spine (check 13) is deliberately 0555/0444 — it imitates the
  # production mirror's "not writable by the launching uid" property — so
  # make it removable again before deleting the scratch tree.
  chmod -R u+w "$SCRATCH" 2>/dev/null
  rm -rf "$SCRATCH"
}
trap cleanup EXIT

# ── build ────────────────────────────────────────────────────────────────
if [ "${1:-}" != "--no-build" ]; then
  say "== building $IMAGE (this pulls debian + npm-installs claude-code)"
  if podman build -t "$IMAGE" -f "$CC_DIR/Containerfile" "$CC_DIR" \
       > "$SCRATCH/build.log" 2>&1; then
    pass "image builds"
  else
    fail "image build (tail of log follows)"; tail -20 "$SCRATCH/build.log"
    exit 1
  fi
fi
say "== image size: $(podman image inspect "$IMAGE" --format '{{.Size}}' \
      | awk '{printf "%.0f MB", $1/1000000}')"

# ── scaffolding: fake broker + scratch mounts ────────────────────────────
mkdir -p "$SCRATCH/home" "$SCRATCH/config"
cp -r "$CC_DIR/config-template/." "$SCRATCH/config/"
chmod 0755 "$SCRATCH/config/hooks/"*.py
# env file: documented shape, fake values only. Includes a marker var so we
# can prove env passthrough without any real key, and BOTH credential names
# with obvious placeholders so the credential-precedence checks below run
# against the real podman rather than a stub. Nothing here is a live secret.
FAKE_OAUTH='sk-ant-oat01-PLACEHOLDER-NOT-A-REAL-TOKEN'
FAKE_APIKEY='sk-ant-api03-PLACEHOLDER-NOT-A-REAL-KEY'
cat > "$SCRATCH/config/env" <<EOF
SMOKE_MARKER=wp-h5
ANTHROPIC_API_KEY=$FAKE_APIKEY
CLAUDE_CODE_OAUTH_TOKEN=$FAKE_OAUTH
EOF
chmod 0600 "$SCRATCH/config/env"

python3 "$CC_DIR/tests/fake_broker.py" "$SCRATCH/broker.sock" \
  --deny read-prod-logs=verb-disabled > "$SCRATCH/fake.log" &
FAKE_PID=$!
for _ in $(seq 50); do [ -S "$SCRATCH/broker.sock" ] && break; sleep 0.1; done
[ -S "$SCRATCH/broker.sock" ] || { fail "fake broker did not come up"; exit 1; }

# run one-shot commands through the real wrapper, scratch everything
in_container() {
  RESIDENT_IMAGE="$IMAGE" \
  RESIDENT_HOME_VOL="$SCRATCH/home" \
  RESIDENT_CONFIG_DIR="$SCRATCH/config" \
  RESIDENT_BROKER_SOCKET="$SCRATCH/broker.sock" \
  RESIDENT_HOUSE_MEMORY="$SCRATCH/nonexistent-house-memory" \
  RESIDENT_NETWORK=none \
  bash "$CC_DIR/run-resident.sh" smoketest "$@" 2>>"$SCRATCH/wrapper.log"
}

# ── the checks ───────────────────────────────────────────────────────────

# 1. broker CLI round trip over the mounted socket
out="$(in_container broker read-own-log --lines 5)"
if [ $? -eq 0 ] && grep -q '"ok": true' <<<"$out" \
   && grep -q 'fake log line 1' <<<"$out"; then
  pass "broker CLI round trip (read-own-log via /run/disjorn-broker/broker.sock)"
else
  fail "broker CLI round trip: $out"
fi

# 2. broker error mapping: denied verb -> exit 12
in_container broker read-prod-logs >/dev/null
[ $? -eq 12 ] && pass "verb-disabled maps to exit 12" \
              || fail "verb-disabled exit code"

# 3. BROKER_DISABLE kill switch refuses without touching the socket
in_container bash -c 'BROKER_DISABLE=1 broker read-metrics' >/dev/null
[ $? -eq 20 ] && pass "BROKER_DISABLE refuses (exit 20)" \
              || fail "BROKER_DISABLE"

# 4. sudo absent
if in_container bash -c 'command -v sudo' >/dev/null; then
  fail "sudo is present in the image"
else
  pass "sudo absent"
fi

# 5. running as non-root 'resident'
u="$(in_container id -un)"
[ "$u" = "resident" ] && pass "container user is 'resident'" \
                       || fail "container user is '$u'"

# 6. /config read-only
if in_container bash -c 'touch /config/x 2>/dev/null'; then
  fail "/config is writable"
else
  pass "/config is read-only"
fi

# 7. managed-settings symlink resolves to the mounted config
if in_container bash -c 'test "$(readlink -f /etc/claude-code/managed-settings.json)" = /config/settings.json && jq -e .permissions.deny /config/settings.json >/dev/null'; then
  pass "managed-settings -> /config/settings.json (valid JSON, deny rules present)"
else
  fail "managed-settings symlink / settings.json"
fi

# 8. env passthrough from the /config env file
v="$(in_container bash -c 'echo "$SMOKE_MARKER"')"
[ "$v" = "wp-h5" ] && pass "env passthrough via --env-file" \
                    || fail "env passthrough (got '$v')"

# 8b. credential precedence, against the REAL podman: with both names in the
#     env file, exactly the OAuth token reaches the container.
cred="$(in_container bash -c 'echo "OAUTH=[${CLAUDE_CODE_OAUTH_TOKEN-unset}] KEY=[${ANTHROPIC_API_KEY-unset}]"')"
if [ "$cred" = "OAUTH=[$FAKE_OAUTH] KEY=[unset]" ]; then
  pass "credential precedence: OAuth token wins, API key not passed ($cred)"
else
  fail "credential precedence: $cred"
fi

# 8c. the wrapper says which credential it used, and never prints its value.
if grep -q 'auth: CLAUDE_CODE_OAUTH_TOKEN' "$SCRATCH/wrapper.log" \
   && grep -q 'NOT passing ANTHROPIC_API_KEY' "$SCRATCH/wrapper.log" \
   && ! grep -qF "$FAKE_OAUTH" "$SCRATCH/wrapper.log"; then
  pass "wrapper announces the credential it used, without printing it"
else
  fail "wrapper credential announcement (see $SCRATCH/wrapper.log)"
fi

# 8d. the token is not in any host process's argv.
#     (The wrapper uses podman's name-only '--env VAR' form precisely so that
#     /proc/*/cmdline never carries an account credential.)
#     NB: the needle must NOT be passed as an argument to the scanner — a
#     `grep -F "$FAKE_OAUTH" /proc/*/cmdline` matches grep's OWN cmdline and
#     reports a phantom leak. Scan from a shell variable instead.
RESIDENT_PODMAN_EXTRA="-d" in_container sleep 30 >/dev/null 2>&1
sleep 1
argv_hit=""
for _cl in /proc/[0-9]*/cmdline; do
  # Brace-group the redirection too: a pid that exits mid-scan makes the
  # SHELL report the failed open, which `tr`'s own 2>/dev/null cannot mute.
  { _c="$(tr '\0' ' ' < "$_cl")"; } 2>/dev/null || continue
  case "$_c" in *"$FAKE_OAUTH"*) argv_hit="$_cl: $_c" ;; esac
done
if [ -n "$argv_hit" ]; then
  fail "OAuth token found in a host process argv -> $argv_hit"
else
  pass "OAuth token absent from every readable /proc/*/cmdline"
fi
# ...but it IS in the container process's environment, by design.
if podman exec resident-cc-smoketest sh -c 'test -n "$CLAUDE_CODE_OAUTH_TOKEN"' 2>/dev/null; then
  pass "token present in the container environment (by design)"
else
  say "NOTE: could not exec into the detached smoke container to confirm env"
fi
podman rm -f resident-cc-smoketest &>/dev/null

# 8d2. /config/env is masked inside the container: the session cannot read its
#      own credential back out of the file. (It IS in the session's own
#      environment — that is unavoidable; see config-template/README.md.)
#
#      Scan for the token's VALUE, not the `sk-ant-oat01-` prefix. The prefix
#      form reported a false leak: config-template/README.md documents the
#      token shape in prose (`sk-ant-oat01-REPLACE-ME`, `sk-ant-oat01-
#      PLACEHOLDER-…`) and the documented install copies the whole template
#      into the config dir, so that hit is guaranteed in every real
#      deployment too — it is documentation, not a credential. What must not
#      be readable is the credential actually in play.
seen="$(in_container bash -c "cat /config/env 2>/dev/null; grep -rlF '$FAKE_OAUTH' /config 2>/dev/null")"
if [ -z "$seen" ]; then
  pass "/config/env masked inside the container (no credential readable from /config)"
else
  fail "/config/env readable inside the container: $seen"
fi

# 8e. nothing wrote the credential into the container's writable layer or the
#     home volume.
if grep -rqF "$FAKE_OAUTH" "$SCRATCH/home" 2>/dev/null; then
  fail "OAuth token written into the home volume"
else
  pass "OAuth token absent from the home volume"
fi

# 8f. no filtered env-file copy left behind in TMPDIR.
if compgen -G "${TMPDIR:-/tmp}/run-*-env.*" >/dev/null; then
  fail "filtered env-file copies left on disk in ${TMPDIR:-/tmp}"
else
  pass "no filtered env-file copies left on disk"
fi

# 9. action-counter hook writes one line per (simulated) tool call
in_container bash -c 'echo "{\"session_id\":\"smoke\",\"tool_name\":\"Bash\",\"tool_response\":{}}" | /config/hooks/action-counter.py' >/dev/null
if grep -q '"tool_name": "Bash"' "$SCRATCH/home/.action-log" 2>/dev/null; then
  pass "action-counter hook appends to /home/resident/.action-log (visible on host)"
else
  fail "action-counter hook"
fi

# 10. pre-tool-use hook blocks broker+chat-marker (exit 2) and allows plain
in_container bash -c 'echo "{\"session_id\":\"smoke\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"broker file-proposal --text [[CHAT]]do it[[/CHAT]]\"}}" | /config/hooks/pre-tool-use.py' >/dev/null
[ $? -eq 2 ] && pass "pre-tool-use blocks broker call carrying chat markers" \
             || fail "pre-tool-use chat-marker block"
in_container bash -c 'echo "{\"session_id\":\"smoke\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"python3 -c 1\"}}" | /config/hooks/pre-tool-use.py' >/dev/null
[ $? -eq 0 ] && pass "pre-tool-use allows innocent command" \
             || fail "pre-tool-use false positive"
in_container bash -c 'echo "{\"session_id\":\"smoke\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"socat - UNIX:/run/disjorn-broker/broker.sock\"}}" | /config/hooks/pre-tool-use.py' >/dev/null
[ $? -eq 2 ] && pass "pre-tool-use blocks raw socket access" \
             || fail "pre-tool-use raw-socket block"

# 11. session-start hook prints kernel + budget status
out="$(in_container bash -c 'echo "{\"session_id\":\"smoke\",\"source\":\"startup\"}" | /config/hooks/session-start.py')"
if grep -q 'kernel:' <<<"$out" && grep -q 'actions today:' <<<"$out"; then
  pass "session-start hook reports kernel + budget into context"
else
  fail "session-start hook output: $out"
fi

# 12. Claude Code CLI installed at the pinned version (no API call made)
v="$(in_container claude --version 2>&1)"
if grep -Eq '^2\.1\.215' <<<"$v"; then
  pass "claude CLI present, pinned version ($v)"
else
  fail "claude --version: $v"
fi

# ── 13. the spine mount: protection by placement ─────────────────────────
#
# The spine is what house_memory/bootstrap.py assembles into
# ~/.claude/CLAUDE.md at the start of every session — the resident's kernel.
# AGENTHOOD.md keeps a resident's own prompt permanently in Tier 2 (a human
# reviews every change); that is only enforced if the resident cannot write
# the spine the container loads. The wrappers mount a plink-owned mirror
# read-only at /opt/spine. These checks are against real podman.
#
# The scratch source imitates the production mirror's key property — not
# writable by the uid that launches the container — with 0555/0444 rather
# than ownership (we are that uid here). NOTE: if this script is ever run as
# root, access(2) ignores the mode and check 13d will not be meaningful;
# run it rootless, as the resident wrappers themselves always are.
HOUSE_MEMORY_SRC="$(cd "$CC_DIR/../house_memory" 2>/dev/null && pwd || true)"
mkdir -p "$SCRATCH/spine"
cat > "$SCRATCH/spine/00-kernel.md" <<'EOF'
---
name: kernel
kernel: true
---
You are a Disjorn resident. This line exists only in the read-only mirror.
EOF
cat > "$SCRATCH/spine/10-people.md" <<'EOF'
---
name: people
---
plink is the human.
EOF
chmod 0444 "$SCRATCH/spine"/*.md
chmod 0555 "$SCRATCH/spine"

in_container_spine() {
  RESIDENT_IMAGE="$IMAGE" \
  RESIDENT_HOME_VOL="$SCRATCH/home" \
  RESIDENT_CONFIG_DIR="$SCRATCH/config" \
  RESIDENT_BROKER_SOCKET="$SCRATCH/broker.sock" \
  RESIDENT_HOUSE_MEMORY="${HOUSE_MEMORY_SRC:-$SCRATCH/nonexistent-house-memory}" \
  RESIDENT_SPINE_HOST="$SCRATCH/spine" \
  RESIDENT_NETWORK=none \
  bash "$CC_DIR/run-resident.sh" smoketest "$@" 2>>"$SCRATCH/wrapper.log"
}

# 13a. mounted and readable from inside
out="$(in_container_spine bash -c 'cat /opt/spine/00-kernel.md')"
if grep -q 'only in the read-only mirror' <<<"$out"; then
  pass "spine mounted and readable at /opt/spine"
else
  fail "spine not readable at /opt/spine: $out"
fi

# 13b. the kernel mount says ro in the container's own mount table
if in_container_spine bash -c 'grep " /opt/spine " /proc/mounts | grep -qw ro'; then
  pass "/opt/spine is ro in the container's /proc/mounts"
else
  fail "/opt/spine mount options: $(in_container_spine bash -c 'grep " /opt/spine " /proc/mounts')"
fi

# 13c. no write of any shape succeeds from inside — this is the whole point
spine_writes="$(in_container_spine bash -c '
  fails=0; tries=0
  try() { tries=$((tries+1)); if eval "$1" 2>/dev/null; then echo "WROTE: $1"; else fails=$((fails+1)); fi; }
  try "echo pwned >> /opt/spine/00-kernel.md"
  try "echo pwned  > /opt/spine/00-kernel.md"
  try "touch /opt/spine/99-selfedit.md"
  try "rm -f /opt/spine/10-people.md"
  try "mv /opt/spine/00-kernel.md /opt/spine/x.md"
  try "mkdir /opt/spine/sub"
  try "ln -s /etc/passwd /opt/spine/98-evil.md"
  try "chmod 0666 /opt/spine/00-kernel.md"
  echo "BLOCKED $fails/$tries"
')"
if grep -q 'BLOCKED 8/8' <<<"$spine_writes" && ! grep -q '^WROTE' <<<"$spine_writes"; then
  pass "spine is unwritable from inside the container (8/8 write shapes blocked)"
else
  fail "spine writable from inside the container: $spine_writes"
fi

# 13d. ...and the host copy is untouched afterwards.
if grep -q 'only in the read-only mirror' "$SCRATCH/spine/00-kernel.md" \
   && [ "$(ls "$SCRATCH/spine" | wc -l)" = 2 ]; then
  pass "host spine unchanged after the write attempts"
else
  fail "host spine changed: $(ls -l "$SCRATCH/spine")"
fi

# 13e. UNSET RESIDENT_SPINE_HOST = today's behaviour: no /opt/spine at all.
#      Both live residents are on this path right now.
if in_container bash -c 'test ! -e /opt/spine'; then
  pass "no spine mount when RESIDENT_SPINE_HOST is unset (unchanged behaviour)"
else
  fail "/opt/spine appeared without RESIDENT_SPINE_HOST"
fi

# 13f. fail CLOSED: a spine source this uid can write must abort the launch.
#      That is the misconfiguration that matters — RESIDENT_SPINE_HOST left
#      pointing at the resident's own home volume, where a ro bind mount
#      protects nothing because the resident edits the HOST path.
mkdir -p "$SCRATCH/writable-spine"
echo "kernel" > "$SCRATCH/writable-spine/00-kernel.md"
if RESIDENT_IMAGE="$IMAGE" RESIDENT_HOME_VOL="$SCRATCH/home" \
   RESIDENT_CONFIG_DIR="$SCRATCH/config" \
   RESIDENT_BROKER_SOCKET="$SCRATCH/broker.sock" \
   RESIDENT_HOUSE_MEMORY="$SCRATCH/nonexistent-house-memory" \
   RESIDENT_SPINE_HOST="$SCRATCH/writable-spine" RESIDENT_NETWORK=none \
   bash "$CC_DIR/run-resident.sh" smoketest true 2>"$SCRATCH/refuse.log"; then
  fail "wrapper launched with a WRITABLE spine source"
elif grep -q 'REFUSING TO LAUNCH' "$SCRATCH/refuse.log"; then
  pass "wrapper refuses to launch with a writable spine source (fails closed, loudly)"
else
  fail "wrapper refused but not loudly: $(cat "$SCRATCH/refuse.log")"
fi

# 13g. end-to-end: bootstrap.py assembles the kernel FROM the read-only
#      mount. This is the cutover, exercised — RESIDENT_SPINE_DIR=/opt/spine
#      is the single line plink adds to the env file.
if [ -n "$HOUSE_MEMORY_SRC" ]; then
  rm -rf "$SCRATCH/home/.claude" "$SCRATCH/home/MEMORY.md"
  out="$(in_container_spine bash -c 'RESIDENT_SPINE_DIR=/opt/spine HOME=/home/resident python3 /opt/house_memory/house_memory/bootstrap.py')"
  if grep -q 'bootstrap: kernel' <<<"$out" \
     && grep -q 'only in the read-only mirror' "$SCRATCH/home/.claude/CLAUDE.md" 2>/dev/null \
     && grep -q 'assembled from /opt/spine' "$SCRATCH/home/.claude/CLAUDE.md" 2>/dev/null; then
    pass "bootstrap.py assembles ~/.claude/CLAUDE.md from the ro /opt/spine mount"
  else
    fail "bootstrap from /opt/spine: $out"
  fi
else
  say "NOTE: house_memory package not found next to harness/cc; skipped 13g"
fi

# ── 14. the container reaper: killing the wrapper kills the session ──────
#
# Rootless `podman run` hands the container to conmon, which is reparented
# away, so killing the podman CLIENT leaves the container Up. Both
# supervisors that stop this wrapper use Python's proc.kill() (SIGKILL):
# residency/launcher.py's pre-act model gate when it REFUSES a session, and
# brokerd.py's build reaper at timeout_sec. Without the watchdog the refused
# session kept writing files and calling the broker inside a container
# nobody was reading from.
#
# 14a. baseline — demonstrate the gap is real on this podman, so a later
#      podman that fixes it upstream shows up here as a NOTE rather than as
#      a silently redundant watchdog.
podman rm -f -t 0 --ignore reaper-baseline &>/dev/null
podman run --rm --name reaper-baseline "$IMAGE" sleep 60 &>/dev/null &
base_pid=$!
for _ in $(seq 50); do
  podman ps --filter name=reaper-baseline --format '{{.Names}}' | grep -q . && break
  sleep 0.2
done
kill -9 "$base_pid" 2>/dev/null
sleep 1.5
if podman ps --filter name=reaper-baseline --format '{{.Names}}' | grep -q reaper-baseline; then
  pass "baseline: SIGKILL of the podman client alone leaves the container Up (the gap is real)"
else
  say "NOTE: this podman already tears the container down when the client dies; the watchdog is belt-and-braces here"
fi
podman rm -f -t 0 --ignore reaper-baseline &>/dev/null

# 14b. the wrapper's watchdog: same SIGKILL, container actually dies.
#      Launch the wrapper DIRECTLY, not through in_container(): backgrounding
#      a shell function makes $! a subshell, and killing that leaves the real
#      wrapper (and its container) alive — which is a broken test, not a
#      broken reaper. $! must be the process that `exec`s podman.
podman rm -f -t 0 --ignore resident-cc-smoketest &>/dev/null
RESIDENT_IMAGE="$IMAGE" \
RESIDENT_HOME_VOL="$SCRATCH/home" \
RESIDENT_CONFIG_DIR="$SCRATCH/config" \
RESIDENT_BROKER_SOCKET="$SCRATCH/broker.sock" \
RESIDENT_HOUSE_MEMORY="$SCRATCH/nonexistent-house-memory" \
RESIDENT_NETWORK=none \
  bash "$CC_DIR/run-resident.sh" smoketest sleep 120 >/dev/null 2>>"$SCRATCH/wrapper.log" &
wrap_pid=$!
up=""
for _ in $(seq 75); do
  if podman ps --filter name=resident-cc-smoketest --format '{{.Names}}' | grep -q .; then up=1; break; fi
  sleep 0.2
done
if [ -z "$up" ]; then
  fail "reaper test: container never came up"
else
  # SIGKILL the wrapper exactly as launcher.py / brokerd.py do. `exec podman`
  # means this pid IS the podman client.
  kill -9 "$wrap_pid" 2>/dev/null
  gone=""
  for _ in $(seq 40); do   # watchdog polls at 0.25s; allow generous slack
    podman ps --filter name=resident-cc-smoketest --format '{{.Names}}' | grep -q . || { gone=1; break; }
    sleep 0.25
  done
  if [ -n "$gone" ]; then
    pass "SIGKILLing the wrapper reaps its container (refused session cannot keep running)"
  else
    fail "container survived the wrapper's SIGKILL: $(podman ps --filter name=resident-cc-smoketest --format '{{.Names}} {{.Status}}')"
  fi
fi
podman rm -f -t 0 --ignore resident-cc-smoketest &>/dev/null
wait "$wrap_pid" 2>/dev/null

# 14c. a DETACHED container must survive the wrapper exiting — that is the
#      success path for `RESIDENT_PODMAN_EXTRA=-d` (check 8d relies on it).
podman rm -f -t 0 --ignore resident-cc-smoketest &>/dev/null
RESIDENT_PODMAN_EXTRA="-d" in_container sleep 30 >/dev/null 2>&1
sleep 2
if podman ps --filter name=resident-cc-smoketest --format '{{.Names}}' | grep -q .; then
  pass "detached container survives the wrapper exiting (watchdog correctly not armed)"
else
  fail "detached container was reaped by the watchdog"
fi
podman rm -f -t 0 --ignore resident-cc-smoketest &>/dev/null

# ── verdict ──────────────────────────────────────────────────────────────
say
if [ "$FAILS" -eq 0 ]; then
  say "ALL CHECKS PASSED"
else
  say "$FAILS CHECK(S) FAILED"
  exit 1
fi
