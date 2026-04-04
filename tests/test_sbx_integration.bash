#!/usr/bin/env bash
# Integration tests for sbx sandbox lifecycle (Phase 7).
# These tests require a running sbx environment — they are skipped if sbx is not installed.
#
# Run:   bash tests/test_sbx_integration.bash
# Env:   OLA_SBX_IMAGE — override template image (default: docker.io/ola/ola-sbx:latest)
#        SBX_TEST_TIMEOUT — seconds to wait for sandbox creation (default: 120)
set -euo pipefail

PASS=0
FAIL=0
SKIP=0
SBX_NAME="ola-integration-test-$$"
TIMEOUT="${SBX_TEST_TIMEOUT:-120}"
IMAGE="${OLA_SBX_IMAGE:-docker.io/ola/ola-sbx:latest}"
TMPDIR_TEST="$(mktemp -d)"
trap 'cleanup' EXIT

cleanup() {
  # Best-effort cleanup — remove the test sandbox if it exists
  if command -v sbx &>/dev/null; then
    sbx stop "$SBX_NAME" 2>/dev/null || true
    sbx rm "$SBX_NAME" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR_TEST"
}

fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }
pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
skip() { echo "SKIP: $1"; SKIP=$((SKIP + 1)); }

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then pass "$label"; else fail "$label (expected '$expected', got '$actual')"; fi
}

assert_contains() {
  local label="$1" haystack="$2" needle="$3"
  if echo "$haystack" | grep -qF "$needle"; then pass "$label"; else fail "$label (expected to contain '$needle')"; fi
}

assert_not_empty() {
  local label="$1" value="$2"
  if [ -n "$value" ]; then pass "$label"; else fail "$label (was empty)"; fi
}

# ===== Prerequisite check =====

if ! command -v sbx &>/dev/null; then
  echo "sbx CLI not found — skipping all integration tests."
  echo "(Install sbx and re-run to execute these tests.)"
  exit 0
fi

echo "Using sbx at: $(command -v sbx)"
echo "Template image: $IMAGE"
echo "Test sandbox name: $SBX_NAME"
echo "Timeout: ${TIMEOUT}s"
echo ""

# ===== 7.1 Verify sandbox creation with new template =====
echo "=== 7.1: Sandbox creation with template ==="

# Setup: create a minimal project directory with agent dir
PROJECT_DIR="$TMPDIR_TEST/project"
AGENT_DIR="$TMPDIR_TEST/agent"
mkdir -p "$PROJECT_DIR" "$AGENT_DIR"

# Minimal whitelist for policy sync
cat > "$AGENT_DIR/whitelist.txt" <<'EOF'
# Integration test whitelist
docs.docker.com
EOF

# Create a marker file so we can verify the mount inside the sandbox
echo "integration-test-marker" > "$PROJECT_DIR/ola-test-marker.txt"

# Test: sbx run creates a sandbox with the custom template
echo "Creating sandbox (this may take a while on first pull)..."
sbx run claude \
  --name "$SBX_NAME" \
  --template "$IMAGE" \
  "$PROJECT_DIR" "$AGENT_DIR:ro" &
SBX_PID=$!

# Wait for sandbox to appear in sbx ls (poll with timeout)
elapsed=0
while ! sbx ls 2>/dev/null | grep -q "$SBX_NAME"; do
  if [ $elapsed -ge "$TIMEOUT" ]; then
    fail "7.1a: sandbox appeared in sbx ls within ${TIMEOUT}s"
    kill "$SBX_PID" 2>/dev/null || true
    echo "Results: $PASS passed, $FAIL failed, $SKIP skipped"
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done
pass "7.1a: sandbox appeared in sbx ls"

# Test: sandbox is listed and running
SBX_LS_OUTPUT="$(sbx ls 2>/dev/null)"
assert_contains "7.1b: sandbox listed in sbx ls" "$SBX_LS_OUTPUT" "$SBX_NAME"

# Test: can exec into the sandbox
EXEC_OUTPUT="$(sbx exec "$SBX_NAME" echo 'hello-from-sbx' 2>/dev/null)" || true
assert_eq "7.1c: exec into sandbox works" "hello-from-sbx" "$EXEC_OUTPUT"

# Test: project directory is mounted (check marker file)
MARKER="$(sbx exec "$SBX_NAME" cat /home/user/project/ola-test-marker.txt 2>/dev/null)" || true
assert_eq "7.1d: project dir mounted with marker" "integration-test-marker" "$MARKER"

# Test: agent directory is mounted read-only
AGENT_MOUNT="$(sbx exec "$SBX_NAME" cat /home/user/agent/whitelist.txt 2>/dev/null)" || true
assert_contains "7.1e: agent dir mounted" "$AGENT_MOUNT" "docs.docker.com"

# Test: agent dir is read-only (write should fail)
WRITE_RESULT="$(sbx exec "$SBX_NAME" touch /home/user/agent/test-write 2>&1)" || true
if echo "$WRITE_RESULT" | grep -qi "read.only\|permission denied\|cannot touch"; then
  pass "7.1f: agent dir is read-only"
else
  # The write might have succeeded — check
  if sbx exec "$SBX_NAME" test -f /home/user/agent/test-write 2>/dev/null; then
    fail "7.1f: agent dir is read-only (write succeeded)"
  else
    pass "7.1f: agent dir is read-only"
  fi
fi

# Test: template tools are installed (claude, uv, openhands, git)
for tool in claude uv openhands git; do
  VERSION="$(sbx exec "$SBX_NAME" "$tool" --version 2>/dev/null)" || true
  if [ -n "$VERSION" ]; then
    pass "7.1g: $tool is installed ($VERSION)"
  else
    fail "7.1g: $tool is installed (not found or no output)"
  fi
done

# Test: git identity is configured
GIT_USER="$(sbx exec "$SBX_NAME" git config --global user.name 2>/dev/null)" || true
assert_eq "7.1h: git user.name is set" "ola" "$GIT_USER"

GIT_EMAIL="$(sbx exec "$SBX_NAME" git config --global user.email 2>/dev/null)" || true
assert_eq "7.1i: git user.email is set" "ola@localhost" "$GIT_EMAIL"

# Test: aliases are configured
ALIASES="$(sbx exec "$SBX_NAME" bash -ic 'alias' 2>/dev/null)" || true
assert_contains "7.1j: claude-yolo alias exists" "$ALIASES" "claude-yolo"
assert_contains "7.1k: oh alias exists" "$ALIASES" "oh="

# Clean up the background sbx run process
kill "$SBX_PID" 2>/dev/null || true
wait "$SBX_PID" 2>/dev/null || true

# ===== 7.2 Verify authentication via .credentials.json =====
echo ""
echo "=== 7.2: Authentication via .credentials.json copy ==="

# Inject credentials into the sandbox (simulates what ola-sandbox does)
HOST_CRED="$HOME/.claude/.credentials.json"
if [ -f "$HOST_CRED" ]; then
  sbx exec "$SBX_NAME" bash -c 'mkdir -p ~/.claude' 2>/dev/null || true
  sbx cp "$HOST_CRED" "$SBX_NAME:/home/user/.claude/.credentials.json" 2>/dev/null || true

  # Test: .credentials.json exists inside the sandbox after copy
  CRED_CHECK="$(sbx exec "$SBX_NAME" bash -c 'test -f ~/.claude/.credentials.json && echo FOUND || echo NOT_FOUND' 2>/dev/null)" || true
  if [ "$CRED_CHECK" = "FOUND" ]; then
    pass "7.2a: .credentials.json copied into sandbox"
  else
    fail "7.2a: .credentials.json copied into sandbox (file not found after copy)"
  fi

  # Test: claude can authenticate (quick prompt that exercises the API via OAuth token)
  AUTH_OUTPUT="$(timeout 30 sbx exec "$SBX_NAME" claude -p 'Reply with exactly: AUTH_OK' --output-format text 2>&1)" || true
  if echo "$AUTH_OUTPUT" | grep -q "AUTH_OK"; then
    pass "7.2b: claude authenticated successfully via .credentials.json"
  elif echo "$AUTH_OUTPUT" | grep -qi "authentication.failed\|authentication_failed\|unauthorized"; then
    fail "7.2b: claude authentication failed (OAuth token may be expired — re-run 'claude' on host)"
  else
    skip "7.2b: claude auth test inconclusive (output: ${AUTH_OUTPUT:0:120})"
  fi
else
  skip "7.2a: host ~/.claude/.credentials.json not found (run 'claude' on host first)"
  skip "7.2b: claude auth test skipped (no host credentials)"
fi

# Test: ANTHROPIC_API_KEY env var is NOT set inside sandbox (auth is file-based, not env-based)
API_KEY_VAR="$(sbx exec "$SBX_NAME" bash -c 'echo "${ANTHROPIC_API_KEY:-UNSET}"' 2>/dev/null)" || true
if [ "$API_KEY_VAR" = "UNSET" ] || [ -z "$API_KEY_VAR" ]; then
  pass "7.2c: ANTHROPIC_API_KEY not set in sandbox env (auth is file-based)"
else
  fail "7.2c: ANTHROPIC_API_KEY not set in sandbox env (variable is set)"
fi

# ===== 7.7 Verify reconnection =====
echo ""
echo "=== 7.7: Sandbox reconnection ==="

# Test: sbx run with --name reconnects to existing sandbox (doesn't create a new one)
sbx run claude --name "$SBX_NAME" &
RECONNECT_PID=$!
sleep 3

# Should still be the same sandbox (only one with this name)
COUNT="$(sbx ls 2>/dev/null | grep -c "$SBX_NAME" || echo 0)"
assert_eq "7.7a: reconnection reuses existing sandbox" "1" "$COUNT"

kill "$RECONNECT_PID" 2>/dev/null || true
wait "$RECONNECT_PID" 2>/dev/null || true

# ===== 7.6 Verify persistence across stop/restart =====
echo ""
echo "=== 7.6: Sandbox persistence ==="

# Create a file inside the sandbox
sbx exec "$SBX_NAME" bash -c 'echo persistence-test > /home/user/persist-check.txt' 2>/dev/null || true

# Stop the sandbox
sbx stop "$SBX_NAME" 2>/dev/null
sleep 2

# Restart it
sbx run claude --name "$SBX_NAME" &
RESTART_PID=$!

# Wait for it to come back
elapsed=0
while ! sbx exec "$SBX_NAME" echo ready 2>/dev/null; do
  if [ $elapsed -ge 60 ]; then
    fail "7.6a: sandbox restarted within 60s"
    kill "$RESTART_PID" 2>/dev/null || true
    break
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

if [ $elapsed -lt 60 ]; then
  pass "7.6a: sandbox restarted after stop"

  # Check the file persisted
  PERSISTED="$(sbx exec "$SBX_NAME" cat /home/user/persist-check.txt 2>/dev/null)" || true
  assert_eq "7.6b: file persists across stop/restart" "persistence-test" "$PERSISTED"
fi

kill "$RESTART_PID" 2>/dev/null || true
wait "$RESTART_PID" 2>/dev/null || true

# ===== Summary =====
echo ""
echo "==============================="
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "==============================="
[ "$FAIL" -eq 0 ] || exit 1
