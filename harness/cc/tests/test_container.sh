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
# can prove env passthrough without any real key.
cat > "$SCRATCH/config/env" <<'EOF'
SMOKE_MARKER=wp-h5
EOF

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

# ── verdict ──────────────────────────────────────────────────────────────
say
if [ "$FAILS" -eq 0 ]; then
  say "ALL CHECKS PASSED"
else
  say "$FAILS CHECK(S) FAILED"
  exit 1
fi
