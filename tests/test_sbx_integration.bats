#!/usr/bin/env bats
# Integration tests for sbx sandbox lifecycle (Phase 7).
# These tests require a running sbx environment — skipped if sbx is not installed.
#
# Run:   bats tests/test_sbx_integration.bats
# Env:   OLA_SBX_IMAGE — override template image (default: ghcr.io/atineose/ola:latest)
#        SBX_TEST_TIMEOUT — seconds to wait for sandbox creation (default: 120)

TIMEOUT="${SBX_TEST_TIMEOUT:-120}"
IMAGE="${OLA_SBX_IMAGE:-ghcr.io/atineose/ola:latest}"

# --- Helpers ---

_sbx_exec() {
  sbx exec "$SBX_NAME" "$@"
}

# Poll 'sbx ls' until $SBX_NAME reaches $1 status (e.g. "stopped"), or fail
# after $TIMEOUT seconds. Replaces fixed sleeps so a slow/wedged transition
# fails the test with a diagnostic instead of stalling indefinitely.
_wait_for_status() {
  local want="$1" deadline=$((SECONDS + TIMEOUT)) line
  while [ "$SECONDS" -lt "$deadline" ]; do
    line="$(sbx ls 2>/dev/null | grep -E "(^|[[:space:]])$SBX_NAME([[:space:]]|$)" || true)"
    [[ "$line" == *"$want"* ]] && return 0
    sleep 1
  done
  echo "Timed out after ${TIMEOUT}s waiting for '$SBX_NAME' to be '$want' (last: ${line:-<absent>})" >&2
  return 1
}

# --- Lifecycle ---

setup_file() {
  if ! command -v sbx &>/dev/null; then
    skip "sbx CLI not found"
  fi

  export SBX_NAME="ola-integration-test"
  export TIMEOUT IMAGE
  export TMPDIR_TEST="$(mktemp -d)"
  export PROJECT_DIR="$TMPDIR_TEST/src"
  export AGENT_DIR="$TMPDIR_TEST/agent"

  mkdir -p "$PROJECT_DIR" "$AGENT_DIR"
  echo "integration-test-marker" > "$PROJECT_DIR/ola-test-marker.txt"
  cat > "$AGENT_DIR/allowlist.txt" <<'EOF'
# Integration test allowlist
docs.docker.com
EOF

  # Create the sandbox non-interactively (shared across all tests in this file)
  # Mount the project dir (parent) so both src/ and agent/ are writable
  local template_flag=()
  if [ -n "$IMAGE" ]; then
    template_flag=(--template "$IMAGE")
  fi

  local create_err
  create_err="$(sbx create shell \
    --name "$SBX_NAME" \
    "${template_flag[@]}" \
    -m 4g \
    -q \
    "$TMPDIR_TEST" 2>&1)" || {
    rm -rf "$TMPDIR_TEST"
    echo "$create_err" >&2
    return 1
  }
}

teardown_file() {
  if command -v sbx &>/dev/null; then
    timeout "$TIMEOUT" sbx stop "$SBX_NAME" 2>/dev/null || true
    # --force: bats runs without a TTY, so 'sbx rm' would otherwise prompt
    # for confirmation, fail, and leak the sandbox into the next run
    # (causing 'already exists' in setup_file).
    timeout "$TIMEOUT" sbx rm --force "$SBX_NAME" 2>/dev/null || true
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
  [[ "$result" == *"hello-from-sbx"* ]]
}

@test "7.1c: project dir is mounted" {
  result="$(_sbx_exec cat "$PROJECT_DIR/ola-test-marker.txt")"
  [[ "$result" == *"integration-test-marker"* ]]
}

@test "7.1d: agent dir is mounted" {
  _sbx_exec cat "$AGENT_DIR/allowlist.txt" | grep -q "docs.docker.com"
}

@test "7.1e: agent dir is writable" {
  _sbx_exec bash -c "touch $AGENT_DIR/test-write && rm $AGENT_DIR/test-write"
}

@test "7.1f: claude is installed" {
  _sbx_exec which claude
}

@test "7.1g: uv is installed" {
  _sbx_exec which uv
}

@test "7.1h: openhands is installed" {
  # Use which instead of --version to avoid OOM from full SDK import
  _sbx_exec which openhands
}

@test "7.1i: git is installed" {
  result="$(_sbx_exec git --version)"
  [ -n "$result" ]
}

@test "7.1j: git user.name is set" {
  result="$(_sbx_exec git config --global user.name)"
  [[ "$result" == *"ola"* ]]
}

@test "7.1k: git user.email is set" {
  result="$(_sbx_exec git config --global user.email)"
  [[ "$result" == *"ola@localhost"* ]]
}

@test "7.1l: claude-yolo alias exists" {
  _sbx_exec bash -c 'grep -q "claude-yolo" $HOME/.bashrc'
}

@test "7.1m: oh alias exists" {
  _sbx_exec bash -c 'grep -q "alias oh=" $HOME/.bashrc'
}

@test "7.1n: codex is installed" {
  _sbx_exec which codex
}

@test "7.1o: cx alias exists" {
  _sbx_exec bash -c 'grep -q "alias cx=" $HOME/.bashrc'
}

# ===== 7.2 Authentication via .credentials.json =====

@test "7.2a: credentials copied into sandbox" {
  local host_cred="$HOME/.claude/.credentials.json"
  [ -f "$host_cred" ] || skip "no host credentials (~/.claude/.credentials.json)"

  _sbx_exec bash -c 'mkdir -p $HOME/.claude'
  local cred_data
  cred_data="$(base64 < "$host_cred")"
  _sbx_exec bash -c "echo '$cred_data' | base64 -d > \$HOME/.claude/.credentials.json"

  result="$(_sbx_exec bash -c 'test -f $HOME/.claude/.credentials.json && echo FOUND || echo NOT_FOUND')"
  [ "$result" = "FOUND" ]
}

@test "7.2b: claude authenticates via OAuth token" {
  local host_cred="$HOME/.claude/.credentials.json"
  [ -f "$host_cred" ] || skip "no host credentials"

  # Ensure credentials are in place
  _sbx_exec bash -c 'test -f $HOME/.claude/.credentials.json' || skip "credentials not copied (run 7.2a first)"

  result="$(timeout 30 sbx exec "$SBX_NAME" claude -p 'hi' --output-format text 2>&1)" || true
  if echo "$result" | grep -qi "authentication.failed\|authentication_failed\|unauthorized"; then
    echo "Auth failed: ${result:0:200}" >&2
    false
  fi
}

@test "7.2c: ANTHROPIC_API_KEY not hardcoded in image" {
  run _sbx_exec bash -c 'echo "APIKEY=${ANTHROPIC_API_KEY:-UNSET}"'
  # The key should be UNSET or "proxy-managed" (injected by sbx at runtime)
  # — never a real API key baked into the image
  [[ "$output" == *"APIKEY=UNSET"* ]] || [[ "$output" == *"APIKEY=proxy-managed"* ]]
}

# ===== 7.7 Reconnection =====

@test "7.7a: reconnection reuses existing sandbox" {
  # Verify only one instance exists
  count="$(sbx ls 2>/dev/null | grep -c "$SBX_NAME" || echo 0)"
  [ "$count" = "1" ]
}

# ===== 7.6 Persistence across stop/restart =====

@test "7.6a: file persists across stop/restart" {
  # Write a file before stopping.
  timeout "$TIMEOUT" sbx exec "$SBX_NAME" \
    bash -c 'echo persistence-test > /tmp/persist-check.txt'

  # Stop, then wait for the sandbox to actually reach 'stopped' rather than
  # a fixed sleep — restarting a still-stopping container otherwise races
  # and blocks 'sbx exec' indefinitely.
  timeout "$TIMEOUT" sbx stop "$SBX_NAME" 2>/dev/null
  _wait_for_status stopped

  # 'sbx exec' auto-starts a stopped sandbox. Bound it so a wedged
  # docker-in-docker boot fails the test with output instead of hanging.
  result="$(timeout "$TIMEOUT" sbx exec "$SBX_NAME" cat /tmp/persist-check.txt)"
  [[ "$result" == *"persistence-test"* ]]
}
