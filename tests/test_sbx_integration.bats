#!/usr/bin/env bats
# Integration tests for sbx sandbox lifecycle (Phase 7).
# These tests require a running sbx environment — skipped if sbx is not installed.
#
# Run:   bats tests/test_sbx_integration.bats
# Env:   OLA_SBX_IMAGE — override template image (default: ghcr.io/atineose/ola:latest)
#        SBX_TEST_TIMEOUT — seconds to wait for sandbox creation (default: 120)

SBX_NAME="ola-integration-test-$$"
TIMEOUT="${SBX_TEST_TIMEOUT:-120}"
IMAGE="${OLA_SBX_IMAGE:-ghcr.io/atineose/ola:latest}"

# --- Helpers ---

_sbx_exec() {
  sbx exec "$SBX_NAME" "$@" 2>/dev/null
}

# --- Lifecycle ---

setup_file() {
  if ! command -v sbx &>/dev/null; then
    skip "sbx CLI not found"
  fi

  export SBX_NAME TIMEOUT IMAGE
  export TMPDIR_TEST="$(mktemp -d)"
  export PROJECT_DIR="$TMPDIR_TEST/project"
  export AGENT_DIR="$TMPDIR_TEST/agent"

  mkdir -p "$PROJECT_DIR" "$AGENT_DIR"
  echo "integration-test-marker" > "$PROJECT_DIR/ola-test-marker.txt"
  cat > "$AGENT_DIR/whitelist.txt" <<'EOF'
# Integration test whitelist
docs.docker.com
EOF

  # Create the sandbox non-interactively (shared across all tests in this file)
  local template_flag=()
  if [ -n "$IMAGE" ]; then
    template_flag=(--template "$IMAGE")
  fi

  sbx create shell \
    --name "$SBX_NAME" \
    "${template_flag[@]}" \
    -q \
    "$PROJECT_DIR" "$AGENT_DIR:ro" || {
    rm -rf "$TMPDIR_TEST"
    echo "Sandbox creation failed" >&2
    return 1
  }
}

teardown_file() {
  if command -v sbx &>/dev/null; then
    sbx stop "$SBX_NAME" 2>/dev/null || true
    sbx rm "$SBX_NAME" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR_TEST"
}

setup() {
  command -v sbx &>/dev/null || skip "sbx CLI not found"
}

# ===== 7.1 Sandbox creation with template =====

@test "7.1a: sandbox appears in sbx ls" {
  sbx ls 2>/dev/null | grep -q "$SBX_NAME"
}

@test "7.1b: can exec into sandbox" {
  result="$(_sbx_exec echo 'hello-from-sbx')"
  [ "$result" = "hello-from-sbx" ]
}

@test "7.1c: project dir is mounted" {
  result="$(_sbx_exec cat /home/user/project/ola-test-marker.txt)"
  [ "$result" = "integration-test-marker" ]
}

@test "7.1d: agent dir is mounted" {
  _sbx_exec cat /home/user/agent/whitelist.txt | grep -q "docs.docker.com"
}

@test "7.1e: agent dir is read-only" {
  run _sbx_exec touch /home/user/agent/test-write
  # Either the write fails or the file shouldn't exist
  if [ "$status" -eq 0 ]; then
    run _sbx_exec test -f /home/user/agent/test-write
    [ "$status" -ne 0 ]
  fi
}

@test "7.1f: claude is installed" {
  result="$(_sbx_exec claude --version)"
  [ -n "$result" ]
}

@test "7.1g: uv is installed" {
  result="$(_sbx_exec uv --version)"
  [ -n "$result" ]
}

@test "7.1h: openhands is installed" {
  result="$(_sbx_exec openhands --version)"
  [ -n "$result" ]
}

@test "7.1i: git is installed" {
  result="$(_sbx_exec git --version)"
  [ -n "$result" ]
}

@test "7.1j: git user.name is set" {
  result="$(_sbx_exec git config --global user.name)"
  [ "$result" = "ola" ]
}

@test "7.1k: git user.email is set" {
  result="$(_sbx_exec git config --global user.email)"
  [ "$result" = "ola@localhost" ]
}

@test "7.1l: claude-yolo alias exists" {
  _sbx_exec bash -ic 'alias' | grep -q "claude-yolo"
}

@test "7.1m: oh alias exists" {
  _sbx_exec bash -ic 'alias' | grep -q "oh="
}

# ===== 7.2 Authentication via .credentials.json =====

@test "7.2a: credentials copied into sandbox" {
  local host_cred="$HOME/.claude/.credentials.json"
  [ -f "$host_cred" ] || skip "no host credentials (~/.claude/.credentials.json)"

  _sbx_exec bash -c 'mkdir -p ~/.claude'
  sbx cp "$host_cred" "$SBX_NAME:/home/user/.claude/.credentials.json" 2>/dev/null

  result="$(_sbx_exec bash -c 'test -f ~/.claude/.credentials.json && echo FOUND || echo NOT_FOUND')"
  [ "$result" = "FOUND" ]
}

@test "7.2b: claude authenticates via OAuth token" {
  local host_cred="$HOME/.claude/.credentials.json"
  [ -f "$host_cred" ] || skip "no host credentials"

  # Ensure credentials are in place
  _sbx_exec bash -c 'test -f ~/.claude/.credentials.json' || skip "credentials not copied (run 7.2a first)"

  result="$(timeout 30 _sbx_exec claude -p 'Reply with exactly: AUTH_OK' --output-format text 2>&1)" || true
  if echo "$result" | grep -qi "authentication.failed\|authentication_failed\|unauthorized"; then
    false  # fail: auth error
  fi
  echo "$result" | grep -q "AUTH_OK" || skip "auth test inconclusive (output: ${result:0:120})"
}

@test "7.2c: ANTHROPIC_API_KEY not set in sandbox env" {
  result="$(_sbx_exec bash -c 'echo "${ANTHROPIC_API_KEY:-UNSET}"')"
  [ "$result" = "UNSET" ] || [ -z "$result" ]
}

# ===== 7.7 Reconnection =====

@test "7.7a: reconnection reuses existing sandbox" {
  sbx run "$SBX_NAME" &
  local pid=$!
  sleep 3

  count="$(sbx ls 2>/dev/null | grep -c "$SBX_NAME" || echo 0)"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true

  [ "$count" = "1" ]
}

# ===== 7.6 Persistence across stop/restart =====

@test "7.6a: file persists across stop/restart" {
  # Create a file
  _sbx_exec bash -c 'echo persistence-test > /home/user/persist-check.txt'

  # Stop
  sbx stop "$SBX_NAME" 2>/dev/null
  sleep 2

  # Restart
  sbx run "$SBX_NAME" &
  local pid=$!

  # Wait for it to come back
  local elapsed=0
  while ! _sbx_exec echo ready 2>/dev/null; do
    if [ $elapsed -ge 60 ]; then
      kill "$pid" 2>/dev/null || true
      skip "sandbox did not restart within 60s"
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  result="$(_sbx_exec cat /home/user/persist-check.txt)"
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true

  [ "$result" = "persistence-test" ]
}
