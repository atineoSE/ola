#!/usr/bin/env bash
# Tests for ola.sh shell functions (_ola_host_from_url, ola-policy-sync, ola-sandbox).
# Run: bash tests/test_ola_sh.bash
set -euo pipefail

PASS=0
FAIL=0
TMPDIR_TEST="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_TEST"' EXIT

fail() { echo "FAIL: $1 (expected '$2', got '$3')"; FAIL=$((FAIL + 1)); }
pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then pass "$label"; else fail "$label" "$expected" "$actual"; fi
}

# --- Mock sbx: records calls to a log file ---
SBX_LOG="$TMPDIR_TEST/sbx_calls.log"
sbx() {
  echo "sbx $*" >> "$SBX_LOG"
}
export -f sbx

# Source ola.sh (skip zsh-specific _OLA_DIR line)
OLA_SH="$(cd "$(dirname "$0")/.." && pwd)/ola.sh"
# We need to handle the zsh-specific line
eval "$(grep -v '%x' "$OLA_SH")"

# ===== _ola_host_from_url tests =====

assert_eq "https URL" "example.com" "$(_ola_host_from_url "https://example.com")"
assert_eq "http URL" "example.com" "$(_ola_host_from_url "http://example.com")"
assert_eq "URL with port" "example.com" "$(_ola_host_from_url "https://example.com:8080")"
assert_eq "URL with path" "example.com" "$(_ola_host_from_url "https://example.com/api/v1")"
assert_eq "URL with port and path" "example.com" "$(_ola_host_from_url "https://example.com:443/path")"
assert_eq "subdomain URL" "api.llm-proxy.dev" "$(_ola_host_from_url "https://api.llm-proxy.dev/v1")"

# ===== ola-policy-sync tests =====

# Setup: agent dir with whitelist
AGENT_DIR="$TMPDIR_TEST/agent"
mkdir -p "$AGENT_DIR"
cat > "$AGENT_DIR/whitelist.txt" <<'EOF'
# Comment line
docs.docker.com
docker.io

EOF

# Setup: .env with URL variables
ENV_FILE="$TMPDIR_TEST/.env"
cat > "$ENV_FILE" <<'EOF'
# Openhands provider
LLM_MODEL="litellm_proxy/minimax-m2.5"
LLM_API_KEY="sk-test123"
LLM_BASE_URL="https://llm-proxy.app.all-hands.dev"

# Localhost should be skipped
# LMNR_BASE_URL=http://localhost:8000

# Quoted URL
CUSTOM_BASE_URL='https://custom-api.example.com:9090/v1'

# Non-URL variables should be ignored
LLM_TIMEOUT="300"
EOF

# Test: whitelist + .env sync
> "$SBX_LOG"  # clear log
output="$(ola-policy-sync "$AGENT_DIR" "$ENV_FILE")"

# Check output message
assert_eq "sync count message" "Synced 4 domain(s) to sbx policy." "$output"

# Check sbx was called with correct domains
assert_eq "whitelist domain 1" \
  "sbx policy allow network docs.docker.com,*.docs.docker.com" \
  "$(sed -n '1p' "$SBX_LOG")"

assert_eq "whitelist domain 2" \
  "sbx policy allow network docker.io,*.docker.io" \
  "$(sed -n '2p' "$SBX_LOG")"

assert_eq ".env LLM_BASE_URL" \
  "sbx policy allow network llm-proxy.app.all-hands.dev,*.llm-proxy.app.all-hands.dev" \
  "$(sed -n '3p' "$SBX_LOG")"

assert_eq ".env CUSTOM_BASE_URL" \
  "sbx policy allow network custom-api.example.com,*.custom-api.example.com" \
  "$(sed -n '4p' "$SBX_LOG")"

# Verify no extra calls (localhost should be skipped, non-URL vars ignored)
LINE_COUNT="$(wc -l < "$SBX_LOG" | tr -d ' ')"
assert_eq "total sbx calls" "4" "$LINE_COUNT"

# Test: missing whitelist (no error, just .env domains)
> "$SBX_LOG"
EMPTY_AGENT="$TMPDIR_TEST/empty_agent"
mkdir -p "$EMPTY_AGENT"
output="$(ola-policy-sync "$EMPTY_AGENT" "$ENV_FILE")"
assert_eq "env-only sync count" "Synced 2 domain(s) to sbx policy." "$output"

# Test: missing .env (just whitelist domains)
> "$SBX_LOG"
output="$(ola-policy-sync "$AGENT_DIR" "$TMPDIR_TEST/nonexistent.env")"
assert_eq "whitelist-only sync count" "Synced 2 domain(s) to sbx policy." "$output"

# Test: localhost and 127.x are skipped
> "$SBX_LOG"
LOCALHOST_ENV="$TMPDIR_TEST/localhost.env"
cat > "$LOCALHOST_ENV" <<'EOF'
LOCAL_BASE_URL=http://localhost:3000
LOOPBACK_BASE_URL=http://127.0.0.1:8080
EOF
output="$(ola-policy-sync "$EMPTY_AGENT" "$LOCALHOST_ENV")"
assert_eq "localhost skipped" "Synced 0 domain(s) to sbx policy." "$output"
LINE_COUNT="$(wc -l < "$SBX_LOG" 2>/dev/null | tr -d ' ')"
# File may not exist if no calls were made
[ -z "$LINE_COUNT" ] && LINE_COUNT=0
assert_eq "no sbx calls for localhost" "0" "$LINE_COUNT"

# ===== ola-policy-review tests =====

# Override sbx mock to simulate policy ls output
sbx() {
  if [ "$1" = "policy" ] && [ "$2" = "ls" ]; then
    cat <<'POLICY'
RULE   TYPE     ACTION  RESOURCE
1      network  allow   *.docker.io
2      network  allow   *.npmjs.org
3      network  allow   *.github.com
4      network  allow   *.googleapis.com
5      network  allow   docs.docker.com
POLICY
    return 0
  fi
  echo "sbx $*" >> "$SBX_LOG"
}
export -f sbx

# Test: all whitelist domains covered
REVIEW_AGENT="$TMPDIR_TEST/review_agent"
mkdir -p "$REVIEW_AGENT"
cat > "$REVIEW_AGENT/whitelist.txt" <<'EOF'
# These should all be in the policy output
docs.docker.com
docker.io
EOF

output="$(ola-policy-review "$REVIEW_AGENT")"
assert_eq "review all covered" "0" "$(echo "$output" | grep -c '^\s*\[MISSING\]')"
assert_eq "review covered count" "2" "$(echo "$output" | grep -c '\[covered\]')"
assert_eq "review summary" "Summary: 2 covered, 0 missing" "$(echo "$output" | tail -1)"

# Test: missing domain detected
cat > "$REVIEW_AGENT/whitelist.txt" <<'EOF'
docs.docker.com
custom-api.example.com
EOF

output="$(ola-policy-review "$REVIEW_AGENT")" || true  # exits 1 when missing
assert_eq "review missing detected" "1" "$(echo "$output" | grep -c '\[MISSING\]')"
assert_eq "review missing domain" \
  "  [MISSING] custom-api.example.com — run: sbx policy allow network \"custom-api.example.com,*.custom-api.example.com\"" \
  "$(echo "$output" | grep '\[MISSING\]')"
assert_eq "review missing summary" "Summary: 1 covered, 1 missing" "$(echo "$output" | tail -1)"

# Test: broad wildcards flagged
output="$(ola-policy-review "$REVIEW_AGENT")"  || true
assert_eq "review flags broad wildcards" "1" "$(echo "$output" | grep 'Broad wildcards' | grep -c 'review')"

# Test: no whitelist file
EMPTY_REVIEW="$TMPDIR_TEST/no_whitelist"
mkdir -p "$EMPTY_REVIEW"
output="$(ola-policy-review "$EMPTY_REVIEW")"
assert_eq "review no whitelist" "1" "$(echo "$output" | grep -c 'No whitelist.txt')"

# Test: sbx not available
sbx() { return 1; }
export -f sbx
output="$(ola-policy-review "$REVIEW_AGENT" 2>&1)" || true
assert_eq "review sbx failure" "1" "$(echo "$output" | grep -c 'failed to list')"

# ===== ola-sandbox tests =====

# Reset sbx mock for sandbox tests
SBX_LOG="$TMPDIR_TEST/sbx_calls.log"

# Create a fake credentials file so _ola_inject_credentials doesn't warn
FAKE_CLAUDE_DIR="$TMPDIR_TEST/fake_home/.claude"
mkdir -p "$FAKE_CLAUDE_DIR"
echo '{"oauth_token":"fake"}' > "$FAKE_CLAUDE_DIR/.credentials.json"
# Override HOME so ola-sandbox finds the fake credentials
ORIG_HOME="$HOME"
export HOME="$TMPDIR_TEST/fake_home"

# --- Test: error when agent dir is missing ---
ISOLATED="$TMPDIR_TEST/isolated/deep/nested"
mkdir -p "$ISOLATED"
pushd "$ISOLATED" > /dev/null  # no ../agent exists at any level
output="$(ola-sandbox test-sbx 2>&1)" || true
assert_eq "sandbox: error when no agent dir" "1" "$(echo "$output" | grep -c 'agent directory not found')"
popd > /dev/null

# --- Test: reconnect to existing sandbox ---
SBX_SANDBOX_DIR="$TMPDIR_TEST/sbx_sandbox"
mkdir -p "$SBX_SANDBOX_DIR/agent"
cat > "$SBX_SANDBOX_DIR/agent/whitelist.txt" <<'EOF'
docs.docker.com
EOF

# Mock sbx: ls returns a match, so ola-sandbox should reconnect
# Also handle exec/cp from _ola_inject_credentials
sbx() {
  echo "sbx $*" >> "$SBX_LOG"
  if [ "$1" = "ls" ]; then
    echo "my-sandbox  running  2h"
    return 0
  fi
}
export -f sbx

> "$SBX_LOG"
pushd "$SBX_SANDBOX_DIR" > /dev/null
mkdir -p code && cd code
ola-sandbox my-sandbox
popd > /dev/null

# Should call: sbx ls, sbx exec (mkdir), sbx cp (credentials), sbx run claude --name
assert_eq "sandbox: reconnect calls sbx ls" \
  "sbx ls" \
  "$(sed -n '1p' "$SBX_LOG")"
assert_eq "sandbox: reconnect injects credentials" \
  "1" "$(grep -c 'sbx cp' "$SBX_LOG")"
# Last call should be sbx run claude --name
LAST_LINE="$(tail -1 "$SBX_LOG")"
assert_eq "sandbox: reconnect calls sbx run" \
  "1" "$(echo "$LAST_LINE" | grep -c 'sbx run claude --name my-sandbox')"

# --- Test: create new sandbox ---
# Mock sbx: first ls returns no match; subsequent ls calls return any sandbox name
# (simulates sandbox becoming ready after sbx run starts)
SBX_LS_CALL_COUNT_FILE="$TMPDIR_TEST/ls_call_count"
SBX_LS_SANDBOX_NAME_FILE="$TMPDIR_TEST/ls_sandbox_name"
sbx() {
  echo "sbx $*" >> "$SBX_LOG"
  if [ "$1" = "ls" ]; then
    local count sandbox_name
    count="$(cat "$SBX_LS_CALL_COUNT_FILE" 2>/dev/null || echo 0)"
    sandbox_name="$(cat "$SBX_LS_SANDBOX_NAME_FILE" 2>/dev/null || echo unknown)"
    count=$((count + 1))
    echo "$count" > "$SBX_LS_CALL_COUNT_FILE"
    if [ "$count" -ge 2 ]; then
      echo "$sandbox_name  running  0s"
    else
      echo "other-sandbox  running  1h"
    fi
    return 0
  fi
}
export -f sbx

> "$SBX_LOG"
echo "0" > "$SBX_LS_CALL_COUNT_FILE"
echo "new-sandbox" > "$SBX_LS_SANDBOX_NAME_FILE"
pushd "$SBX_SANDBOX_DIR/code" > /dev/null
ola-sandbox new-sandbox
popd > /dev/null

# Should call: sbx ls (no match), sbx policy set-default balanced,
# ola-policy-sync calls, sbx run claude (backgrounded),
# sbx ls (poll → match), sbx exec + sbx cp (credentials), wait
assert_eq "sandbox: new calls sbx ls" \
  "sbx ls" \
  "$(sed -n '1p' "$SBX_LOG")"
assert_eq "sandbox: new sets balanced policy" \
  "sbx policy set-default balanced" \
  "$(sed -n '2p' "$SBX_LOG")"
# policy-sync adds whitelist domain (docs.docker.com)
assert_eq "sandbox: new syncs whitelist" \
  "sbx policy allow network docs.docker.com,*.docs.docker.com" \
  "$(sed -n '3p' "$SBX_LOG")"
# Check sbx run was called with correct args
assert_eq "sandbox: new calls sbx run with --name" \
  "1" "$(grep -c '\-\-name new-sandbox' "$SBX_LOG")"
assert_eq "sandbox: new calls sbx run with --template" \
  "1" "$(grep -c '\-\-template docker.io/ola/ola-sbx:latest' "$SBX_LOG")"
assert_eq "sandbox: new calls sbx run with agent:ro" \
  "1" "$(grep -c 'agent:ro' "$SBX_LOG")"
# Credentials were injected
assert_eq "sandbox: new injects credentials" \
  "1" "$(grep -c 'sbx cp' "$SBX_LOG")"

# --- Test: OLA_SBX_IMAGE override ---
> "$SBX_LOG"
echo "0" > "$SBX_LS_CALL_COUNT_FILE"
echo "custom-sandbox" > "$SBX_LS_SANDBOX_NAME_FILE"
pushd "$SBX_SANDBOX_DIR/code" > /dev/null
OLA_SBX_IMAGE="myregistry.io/custom:v2" ola-sandbox custom-sandbox
popd > /dev/null

assert_eq "sandbox: custom image override" \
  "1" "$(grep -c '\-\-template myregistry.io/custom:v2' "$SBX_LOG")"

# Restore HOME
export HOME="$ORIG_HOME"

# ===== Summary =====
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
