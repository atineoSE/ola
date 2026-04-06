#!/usr/bin/env bats
# Tests for ola.sh shell functions (_ola_host_from_url, ola-policy-sync, ola-sandbox).
# Run: bats tests/test_ola_sh.bats
# Requires: bats-core (brew install bats-core)

setup_file() {
  export TMPDIR_TEST="$(mktemp -d)"
  export SBX_LOG="$TMPDIR_TEST/sbx_calls.log"

  # Fake credentials for _ola_inject_credentials
  mkdir -p "$TMPDIR_TEST/fake_home/.claude"
  echo '{"oauth_token":"fake"}' > "$TMPDIR_TEST/fake_home/.claude/.credentials.json"

  # Shared fixtures
  export AGENT_DIR="$TMPDIR_TEST/agent"
  mkdir -p "$AGENT_DIR"
  cat > "$AGENT_DIR/allowlist.txt" <<'EOF'
# Comment line
docs.docker.com
docker.io

EOF

  export ENV_FILE="$TMPDIR_TEST/.env"
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
}

teardown_file() {
  rm -rf "$TMPDIR_TEST"
}

setup() {
  # Isolate HOME so real host config doesn't leak into tests
  export HOME="$TMPDIR_TEST/fake_home"

  # Re-source ola.sh (functions don't survive subshells in bats)
  local ola_sh="$(cd "$BATS_TEST_DIRNAME/.." && pwd)/ola.sh"
  eval "$(grep -v '%x' "$ola_sh")"

  # Default sbx mock
  sbx() { echo "sbx $*" >> "$SBX_LOG"; }
  export -f sbx

  > "$SBX_LOG"
}

# ===== _ola_host_from_url =====

@test "host_from_url: https" {
  [ "$(_ola_host_from_url "https://example.com")" = "example.com" ]
}

@test "host_from_url: http" {
  [ "$(_ola_host_from_url "http://example.com")" = "example.com" ]
}

@test "host_from_url: strips port" {
  [ "$(_ola_host_from_url "https://example.com:8080")" = "example.com" ]
}

@test "host_from_url: strips path" {
  [ "$(_ola_host_from_url "https://example.com/api/v1")" = "example.com" ]
}

@test "host_from_url: strips port and path" {
  [ "$(_ola_host_from_url "https://example.com:443/path")" = "example.com" ]
}

@test "host_from_url: subdomain" {
  [ "$(_ola_host_from_url "https://api.llm-proxy.dev/v1")" = "api.llm-proxy.dev" ]
}

# ===== ola-policy-sync =====

@test "policy-sync: allowlist + env syncs 4 domains" {
  run ola-policy-sync "$AGENT_DIR" "$ENV_FILE"
  [ "$status" -eq 0 ]
  [ "$output" = "Synced 4 domain(s) to sbx policy." ]
}

@test "policy-sync: allowlist domain 1" {
  ola-policy-sync "$AGENT_DIR" "$ENV_FILE"
  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx policy allow network docs.docker.com,*.docs.docker.com" ]
}

@test "policy-sync: allowlist domain 2" {
  ola-policy-sync "$AGENT_DIR" "$ENV_FILE"
  [ "$(sed -n '2p' "$SBX_LOG")" = "sbx policy allow network docker.io,*.docker.io" ]
}

@test "policy-sync: env LLM_BASE_URL" {
  ola-policy-sync "$AGENT_DIR" "$ENV_FILE"
  [ "$(sed -n '3p' "$SBX_LOG")" = "sbx policy allow network llm-proxy.app.all-hands.dev,*.llm-proxy.app.all-hands.dev" ]
}

@test "policy-sync: env CUSTOM_BASE_URL" {
  ola-policy-sync "$AGENT_DIR" "$ENV_FILE"
  [ "$(sed -n '4p' "$SBX_LOG")" = "sbx policy allow network custom-api.example.com,*.custom-api.example.com" ]
}

@test "policy-sync: exactly 4 sbx calls" {
  ola-policy-sync "$AGENT_DIR" "$ENV_FILE"
  [ "$(wc -l < "$SBX_LOG" | tr -d ' ')" = "4" ]
}

@test "policy-sync: env-only (no allowlist)" {
  mkdir -p "$TMPDIR_TEST/empty_agent"
  run ola-policy-sync "$TMPDIR_TEST/empty_agent" "$ENV_FILE"
  [ "$status" -eq 0 ]
  [ "$output" = "Synced 2 domain(s) to sbx policy." ]
}

@test "policy-sync: allowlist-only (no env)" {
  run ola-policy-sync "$AGENT_DIR" "$TMPDIR_TEST/nonexistent.env"
  [ "$status" -eq 0 ]
  [ "$output" = "Synced 2 domain(s) to sbx policy." ]
}

@test "policy-sync: localhost and 127.x are skipped" {
  cat > "$TMPDIR_TEST/localhost.env" <<'EOF'
LOCAL_BASE_URL=http://localhost:3000
LOOPBACK_BASE_URL=http://127.0.0.1:8080
EOF
  mkdir -p "$TMPDIR_TEST/empty_agent"
  run ola-policy-sync "$TMPDIR_TEST/empty_agent" "$TMPDIR_TEST/localhost.env"
  [ "$status" -eq 0 ]
  [ "$output" = "Synced 0 domain(s) to sbx policy." ]
}

# ===== ola-policy-review =====

_mock_sbx_policy_ls() {
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
}

@test "policy-review: all domains covered" {
  _mock_sbx_policy_ls
  local review_agent="$TMPDIR_TEST/review_covered"
  mkdir -p "$review_agent"
  cat > "$review_agent/allowlist.txt" <<'EOF'
docs.docker.com
docker.io
EOF
  run ola-policy-review "$review_agent"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Summary: 2 covered, 0 missing"* ]]
  [[ "$output" != *"[MISSING]"* ]]
}

@test "policy-review: missing domain detected" {
  _mock_sbx_policy_ls
  local review_agent="$TMPDIR_TEST/review_missing"
  mkdir -p "$review_agent"
  cat > "$review_agent/allowlist.txt" <<'EOF'
docs.docker.com
custom-api.example.com
EOF
  run ola-policy-review "$review_agent"
  [ "$status" -ne 0 ]
  [[ "$output" == *"[MISSING] custom-api.example.com"* ]]
  [[ "$output" == *"Summary: 1 covered, 1 missing"* ]]
}

@test "policy-review: broad wildcards flagged" {
  _mock_sbx_policy_ls
  local review_agent="$TMPDIR_TEST/review_broad"
  mkdir -p "$review_agent"
  echo "docs.docker.com" > "$review_agent/allowlist.txt"
  run ola-policy-review "$review_agent"
  [[ "$output" == *"Broad wildcards"* ]]
}

@test "policy-review: no allowlist file" {
  _mock_sbx_policy_ls
  mkdir -p "$TMPDIR_TEST/no_allowlist"
  run ola-policy-review "$TMPDIR_TEST/no_allowlist"
  [ "$status" -eq 0 ]
  [[ "$output" == *"No allowlist.txt"* ]]
}

@test "policy-review: sbx failure" {
  sbx() { return 1; }
  export -f sbx
  mkdir -p "$TMPDIR_TEST/review_sbxfail"
  echo "example.com" > "$TMPDIR_TEST/review_sbxfail/allowlist.txt"
  run ola-policy-review "$TMPDIR_TEST/review_sbxfail"
  [ "$status" -ne 0 ]
  [[ "$output" == *"failed to list"* ]]
}

# ===== ola-sandbox =====

@test "sandbox: error when agent dir missing" {
  mkdir -p "$TMPDIR_TEST/isolated/deep/nested"
  cd "$TMPDIR_TEST/isolated/deep/nested"
  run ola-sandbox test-sbx
  [ "$status" -ne 0 ]
  [[ "$output" == *"agent directory not found"* ]]
}

@test "sandbox: reconnect to existing sandbox" {
  mkdir -p "$TMPDIR_TEST/sbx_reconnect/agent" "$TMPDIR_TEST/sbx_reconnect/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_reconnect/agent/allowlist.txt"

  # Mock security (macOS Keychain) for cc-credentials
  security() { echo '{"oauth_token":"fake"}'; }
  export -f security

  sbx() {
    echo "sbx $*" >> "$SBX_LOG"
    if [ "$1" = "ls" ]; then
      echo "my-sandbox  running  2h"
      return 0
    fi
  }
  export -f sbx

  cd "$TMPDIR_TEST/sbx_reconnect/code"
  ola-sandbox my-sandbox

  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx ls" ]
  # Credentials injected via sbx exec
  grep -q 'sbx exec my-sandbox bash' "$SBX_LOG"
  [[ "$(tail -1 "$SBX_LOG")" == *"sbx run my-sandbox"* ]]
}

_mock_sbx_new_sandbox() {
  local sandbox_name="$1"

  # Mock security (macOS Keychain) for cc-credentials
  security() { echo '{"oauth_token":"fake"}'; }
  export -f security

  eval "
  sbx() {
    echo \"sbx \$*\" >> \"$SBX_LOG\"
    if [ \"\$1\" = \"ls\" ]; then
      echo 'other-sandbox  running  1h'
      return 0
    fi
  }
  export -f sbx
  "
}

@test "sandbox: create new sandbox" {
  mkdir -p "$TMPDIR_TEST/sbx_new/agent" "$TMPDIR_TEST/sbx_new/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_new/agent/allowlist.txt"

  _mock_sbx_new_sandbox "new-sandbox"

  cd "$TMPDIR_TEST/sbx_new/code"
  ola-sandbox new-sandbox

  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx ls" ]
  [ "$(sed -n '2p' "$SBX_LOG")" = "sbx policy set-default balanced" ]
  grep -q "sbx policy allow network docs.docker.com" "$SBX_LOG"
  grep -q "sbx create shell --name new-sandbox --template ghcr.io/$(whoami)/ola:latest -q" "$SBX_LOG"
  # Project dir (parent of code/) is the single workspace — no :ro
  grep -q "sbx_new$" "$SBX_LOG"
  ! grep -q 'agent:ro' "$SBX_LOG"
  grep -q "sbx exec new-sandbox bash" "$SBX_LOG"
  grep -q 'sbx run new-sandbox' "$SBX_LOG"
}

@test "sandbox: OLA_SBX_IMAGE override" {
  mkdir -p "$TMPDIR_TEST/sbx_custom/agent" "$TMPDIR_TEST/sbx_custom/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_custom/agent/allowlist.txt"

  _mock_sbx_new_sandbox "custom-sandbox"

  cd "$TMPDIR_TEST/sbx_custom/code"
  OLA_SBX_IMAGE="myregistry.io/custom:v2" ola-sandbox custom-sandbox

  grep -q '\--template myregistry.io/custom:v2' "$SBX_LOG"
  grep -q 'sbx create shell' "$SBX_LOG"
}
