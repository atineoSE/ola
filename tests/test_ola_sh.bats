#!/usr/bin/env bats
# Tests for ola.sh shell helpers.
# Env expansion is owned by python-dotenv (see tests/test_envresolve.py);
# ola.sh only consumes `ola env` output, applies the sbx network policy, and
# injects the resolved snapshot. `ola` and `sbx` are mocked here.
# Run: bats tests/test_ola_sh.bats   (requires bats-core)

setup_file() {
  export TMPDIR_TEST="$(mktemp -d)"
  export SBX_LOG="$TMPDIR_TEST/sbx_calls.log"

  # Fake credentials for _ola_inject_credentials
  mkdir -p "$TMPDIR_TEST/fake_home/.claude"
  echo '{"oauth_token":"fake"}' > "$TMPDIR_TEST/fake_home/.claude/.credentials.json"

  export AGENT_DIR="$TMPDIR_TEST/agent"
  mkdir -p "$AGENT_DIR"
  cat > "$AGENT_DIR/allowlist.txt" <<'EOF'
# Comment line
docs.docker.com
docker.io

EOF
}

teardown_file() {
  rm -rf "$TMPDIR_TEST"
}

setup() {
  export HOME="$TMPDIR_TEST/fake_home"
  unset OLA_SBX_IMAGE

  # Re-source ola.sh (functions don't survive subshells in bats)
  local ola_sh="$(cd "$BATS_TEST_DIRNAME/.." && pwd)/ola.sh"
  eval "$(grep -v '%x' "$ola_sh")"

  # Default sbx mock — logs every call.
  sbx() { echo "sbx $*" >> "$SBX_LOG"; }
  export -f sbx

  # Default `ola` mock. `ola env` emits $OLA_ENV_BLOB (if set) and exits
  # $OLA_ENV_RC (default 0), standing in for the python resolver, which is
  # unit-tested separately. Any other ola call is logged.
  ola() {
    if [ "$1" = "env" ]; then
      [ -n "${OLA_ENV_BLOB:-}" ] && printf '%s\n' "$OLA_ENV_BLOB"
      return "${OLA_ENV_RC:-0}"
    fi
    echo "ola $*" >> "$SBX_LOG"
  }
  export -f ola
  unset OLA_ENV_BLOB OLA_ENV_RC

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

# ===== _ola_port_from_url =====

@test "port_from_url: extracts port" {
  [ "$(_ola_port_from_url "https://example.com:8080/path")" = "8080" ]
}

@test "port_from_url: no port returns empty" {
  [ "$(_ola_port_from_url "https://example.com/path")" = "" ]
}

@test "port_from_url: localhost with port" {
  [ "$(_ola_port_from_url "http://localhost:11434/v1")" = "11434" ]
}

# ===== _ola_allow_host (IP literal must NOT get a *.<ip> wildcard) =====

@test "allow_host: domain gets wildcard subdomain" {
  _ola_allow_host "llm.example.com"
  [ "$(cat "$SBX_LOG")" = "sbx policy allow network llm.example.com,*.llm.example.com" ]
}

@test "allow_host: IPv4 literal allowed bare (no *.ip)" {
  _ola_allow_host "216.243.220.30"
  [ "$(cat "$SBX_LOG")" = "sbx policy allow network 216.243.220.30" ]
}

@test "allow_host: empty host is a no-op" {
  _ola_allow_host ""
  [ ! -s "$SBX_LOG" ]
}

# ===== _ola_blob_val =====

@test "blob_val: extracts a quoted value" {
  local blob='LLM_MODEL="openai/qwen3.5"
LLM_BASE_URL="https://10.0.0.5/v1"'
  [ "$(_ola_blob_val "$blob" LLM_BASE_URL)" = "https://10.0.0.5/v1" ]
}

@test "blob_val: missing key yields empty" {
  [ "$(_ola_blob_val 'A="1"' NOPE)" = "" ]
}

@test "blob_val: reverses escaped quote/backslash" {
  local blob='K="a\"b\\c"'
  [ "$(_ola_blob_val "$blob" K)" = 'a"b\c' ]
}

# ===== _ola_apply_policy =====

@test "apply_policy: resolved IP LLM endpoint allowed bare (regression)" {
  local blob='LLM_BASE_URL="https://216.243.220.30/v1"'
  mkdir -p "$TMPDIR_TEST/ap_ip"
  run _ola_apply_policy "$TMPDIR_TEST/ap_ip" "$blob"
  [ "$status" -eq 0 ]
  [ "$output" = "Synced 1 domain(s) to sbx policy." ]
  [ "$(cat "$SBX_LOG")" = "sbx policy allow network 216.243.220.30" ]
}

@test "apply_policy: resolved domain LLM endpoint gets wildcard" {
  local blob='LLM_BASE_URL="https://llm-proxy.app.all-hands.dev"'
  mkdir -p "$TMPDIR_TEST/ap_dom"
  run _ola_apply_policy "$TMPDIR_TEST/ap_dom" "$blob"
  [ "$output" = "Synced 1 domain(s) to sbx policy." ]
  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx policy allow network llm-proxy.app.all-hands.dev,*.llm-proxy.app.all-hands.dev" ]
}

@test "apply_policy: allowlist.txt + LLM endpoint counted together" {
  local blob='LLM_BASE_URL="https://216.243.220.30/v1"'
  run _ola_apply_policy "$AGENT_DIR" "$blob"
  [ "$output" = "Synced 3 domain(s) to sbx policy." ]
  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx policy allow network docs.docker.com,*.docs.docker.com" ]
  [ "$(sed -n '2p' "$SBX_LOG")" = "sbx policy allow network docker.io,*.docker.io" ]
  [ "$(sed -n '3p' "$SBX_LOG")" = "sbx policy allow network 216.243.220.30" ]
}

@test "apply_policy: strips inline comments from allowlist (regression)" {
  # An inline '# note' must not leak into RESOURCES. A real allowlist line
  # 'www.eventbriteapi.com  # ... ticket_classes ...' once passed the whole
  # comment to sbx, which split it on commas/spaces and parsed 'ticket_classes'
  # as a bogus domain → duplicate-rule error → the entire sync aborted.
  mkdir -p "$TMPDIR_TEST/ap_inline"
  cat > "$TMPDIR_TEST/ap_inline/allowlist.txt" <<'EOF'
# full-line comment
www.eventbriteapi.com          # v3 REST API base (events, ticket_classes, attendees)
   api.example.com   # indented host with trailing note
EOF
  run _ola_apply_policy "$TMPDIR_TEST/ap_inline" ""
  [ "$status" -eq 0 ]
  [ "$output" = "Synced 2 domain(s) to sbx policy." ]
  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx policy allow network www.eventbriteapi.com,*.www.eventbriteapi.com" ]
  [ "$(sed -n '2p' "$SBX_LOG")" = "sbx policy allow network api.example.com,*.api.example.com" ]
}

@test "apply_policy: LLM localhost allows with port" {
  local blob='LLM_BASE_URL="http://localhost:11434/v1"'
  mkdir -p "$TMPDIR_TEST/ap_local"
  run _ola_apply_policy "$TMPDIR_TEST/ap_local" "$blob"
  [ "$output" = "Synced 1 domain(s) to sbx policy." ]
  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx policy allow network localhost:11434" ]
}

@test "apply_policy: LMNR localhost with port" {
  local blob='LMNR_BASE_URL="http://localhost:8000"
LMNR_HTTP_PORT="8000"'
  mkdir -p "$TMPDIR_TEST/ap_lmnr"
  run _ola_apply_policy "$TMPDIR_TEST/ap_lmnr" "$blob"
  [ "$output" = "Synced 1 domain(s) to sbx policy." ]
  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx policy allow network localhost:8000" ]
}

@test "apply_policy: LMNR remote domain" {
  local blob='LMNR_BASE_URL="https://api.lmnr.ai"'
  mkdir -p "$TMPDIR_TEST/ap_lmnr2"
  run _ola_apply_policy "$TMPDIR_TEST/ap_lmnr2" "$blob"
  [ "$output" = "Synced 1 domain(s) to sbx policy." ]
  [ "$(sed -n '1p' "$SBX_LOG")" = "sbx policy allow network api.lmnr.ai,*.api.lmnr.ai" ]
}

@test "apply_policy: empty blob → allowlist only" {
  run _ola_apply_policy "$AGENT_DIR" ""
  [ "$output" = "Synced 2 domain(s) to sbx policy." ]
}

@test "apply_policy: no allowlist, empty blob → 0" {
  mkdir -p "$TMPDIR_TEST/ap_none"
  run _ola_apply_policy "$TMPDIR_TEST/ap_none" ""
  [ "$output" = "Synced 0 domain(s) to sbx policy." ]
}

@test "apply_policy: returns non-zero and reports when sbx policy allow fails" {
  # Simulates the v0.29.0 breaking change where the bare command form
  # exits non-zero — must surface, not print a bogus "Synced" success.
  sbx() {
    [ "$1 $2 $3" = "policy allow network" ] && return 1
    echo "sbx $*" >> "$SBX_LOG"
  }
  export -f sbx
  local blob='LLM_BASE_URL="https://216.243.220.30/v1"'
  mkdir -p "$TMPDIR_TEST/ap_fail"
  run _ola_apply_policy "$TMPDIR_TEST/ap_fail" "$blob"
  [ "$status" -ne 0 ]
  [[ "$output" != *"Synced"* ]]
  [[ "$output" == *"failed to apply"* ]]
}

# ===== ola-policy-sync (delegates to `ola env`) =====

@test "policy-sync: uses resolved blob from ola env" {
  mkdir -p "$TMPDIR_TEST/ps_ok"
  export OLA_ENV_BLOB='LLM_BASE_URL="https://216.243.220.30/v1"'
  run ola-policy-sync "$TMPDIR_TEST/ps_ok"
  [ "$status" -eq 0 ]
  [ "$output" = "Synced 1 domain(s) to sbx policy." ]
  [ "$(cat "$SBX_LOG")" = "sbx policy allow network 216.243.220.30" ]
}

@test "policy-sync: fail-fast when ola env exits non-zero" {
  mkdir -p "$TMPDIR_TEST/ps_fail"
  export OLA_ENV_RC=1
  run ola-policy-sync "$TMPDIR_TEST/ps_fail"
  [ "$status" -ne 0 ]
  [[ "$output" != *"Synced"* ]]
  [ ! -s "$SBX_LOG" ]
}

@test "policy-sync: missing agent dir errors" {
  mkdir -p "$TMPDIR_TEST/ps_noagent/deep"
  cd "$TMPDIR_TEST/ps_noagent/deep"
  run ola-policy-sync
  [ "$status" -ne 0 ]
  [[ "$output" == *"agent directory not found"* ]]
}

# ===== _ola_inject_sidecar =====

@test "inject_sidecar: writes resolved snapshot to ~/.ola/agent.env" {
  _ola_inject_sidecar box 'LLM_API_KEY="tok"'
  grep -q 'sbx exec box bash -c mkdir -p "$HOME/.ola"' "$SBX_LOG"
  grep -q '\.ola/agent.env' "$SBX_LOG"
}

@test "inject_sidecar: empty blob is a no-op" {
  _ola_inject_sidecar box ""
  [ ! -s "$SBX_LOG" ]
}

# ===== _ola_inject_oh_settings / _ola_setup_shell_rc =====

# Minimal but schema-faithful agent_settings.json (llm + nested
# condenser.llm + preserved tools/kind/usage_id).
_oh_template() {
  cat <<'JSON'
{
  "llm": {"model":"old/m","api_key":"OLD","base_url":"https://1.2.3.4/v1","usage_id":"agent","timeout":300},
  "tools": [{"name":"terminal","params":{}}],
  "condenser": {"llm":{"model":"old/m","api_key":"OLD","base_url":"https://1.2.3.4/v1","usage_id":"condenser"},"kind":"LLMSummarizingCondenser"},
  "kind": "Agent"
}
JSON
}

# Decode the base64 payload written to <dest> from the sbx call log.
_decode_written() {
  local dest="$1" line b64
  line="$(grep "$dest" "$SBX_LOG" | grep 'base64 -d' | tail -1)"
  b64="$(printf '%s' "$line" | sed -E "s/.*echo '([A-Za-z0-9+/=]+)'.*/\1/")"
  printf '%s' "$b64" | base64 -d
}

@test "inject_oh_settings: patches llm + condenser.llm, preserves the rest" {
  export HOME="$TMPDIR_TEST/oh_patch_home"
  mkdir -p "$HOME/.openhands"
  _oh_template > "$HOME/.openhands/agent_settings.json"
  local blob='LLM_MODEL="openai/qwen3.5"
LLM_API_KEY="RESKEY"
LLM_BASE_URL="https://10.0.0.9/v1"'
  _ola_inject_oh_settings box "$blob"
  local out
  out="$(_decode_written agent_settings.json)"
  [ "$(echo "$out" | jq -r '.llm.model')" = "openai/qwen3.5" ]
  [ "$(echo "$out" | jq -r '.llm.api_key')" = "RESKEY" ]
  [ "$(echo "$out" | jq -r '.llm.base_url')" = "https://10.0.0.9/v1" ]
  [ "$(echo "$out" | jq -r '.condenser.llm.base_url')" = "https://10.0.0.9/v1" ]
  # untouched fields survive the patch
  [ "$(echo "$out" | jq -r '.llm.usage_id')" = "agent" ]
  [ "$(echo "$out" | jq -r '.condenser.llm.usage_id')" = "condenser" ]
  [ "$(echo "$out" | jq -r '.llm.timeout')" = "300" ]
  [ "$(echo "$out" | jq -r '.kind')" = "Agent" ]
}

@test "inject_oh_settings: no host template is a no-op" {
  export HOME="$TMPDIR_TEST/oh_none_home"
  mkdir -p "$HOME"
  _ola_inject_oh_settings box 'LLM_MODEL="m"
LLM_API_KEY="k"
LLM_BASE_URL="u"'
  ! grep -q 'agent_settings.json' "$SBX_LOG"
}

@test "inject_oh_settings: missing resolved value is a no-op" {
  export HOME="$TMPDIR_TEST/oh_partial_home"
  mkdir -p "$HOME/.openhands"
  _oh_template > "$HOME/.openhands/agent_settings.json"
  _ola_inject_oh_settings box 'LLM_MODEL="m"
LLM_API_KEY="k"'
  ! grep -q 'agent_settings.json' "$SBX_LOG"
}

@test "setup_shell_rc: exports sidecar, mirrors TLS skip, then cd" {
  _ola_setup_shell_rc box /work/petclinic
  local out
  out="$(_decode_written .bashrc)"
  echo "$out" | grep -qF 'set -a'
  echo "$out" | grep -qF '. "$HOME/.ola/agent.env"'
  echo "$out" | grep -qF 'export SSL_VERIFY=False'
  [ "$(echo "$out" | tail -1)" = "cd /work/petclinic" ]
  # $HOME / ${LLM_SKIP_TLS_VERIFY} must stay literal (expand in-sandbox)
  echo "$out" | grep -qF '$HOME/.ola/agent.env'
  echo "$out" | grep -qF '${LLM_SKIP_TLS_VERIFY:-}'
}

# ===== ola-policy-review (unchanged behaviour) =====

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

@test "sandbox: aborts when ola env fails (host env unsound)" {
  mkdir -p "$TMPDIR_TEST/sbx_envfail/agent" "$TMPDIR_TEST/sbx_envfail/code"
  security() { echo '{"oauth_token":"fake"}'; }
  export -f security
  sbx() {
    echo "sbx $*" >> "$SBX_LOG"
    [ "$1" = "ls" ] && { echo "other  running  1h"; return 0; }
  }
  export -f sbx
  export OLA_ENV_RC=1

  cd "$TMPDIR_TEST/sbx_envfail/code"
  run ola-sandbox envfail-sbx
  [ "$status" -ne 0 ]
  [[ "$output" == *"validation failed"* ]]
  ! grep -q 'sbx create' "$SBX_LOG"
  ! grep -q 'sbx run' "$SBX_LOG"
}

@test "sandbox: aborts when sbx policy sync fails (no create/run)" {
  mkdir -p "$TMPDIR_TEST/sbx_polfail/agent" "$TMPDIR_TEST/sbx_polfail/code"
  security() { echo '{"oauth_token":"fake"}'; }
  export -f security
  sbx() {
    [ "$1" = "ls" ] && { echo "other  running  1h"; return 0; }
    [ "$1 $2 $3" = "policy allow network" ] && return 1
    echo "sbx $*" >> "$SBX_LOG"
  }
  export -f sbx
  export OLA_ENV_BLOB='LLM_BASE_URL="https://216.243.220.30/v1"'

  cd "$TMPDIR_TEST/sbx_polfail/code"
  run ola-sandbox polfail-sbx
  [ "$status" -ne 0 ]
  [[ "$output" == *"network policy sync failed"* ]]
  ! grep -q 'sbx create' "$SBX_LOG"
  ! grep -q 'sbx run' "$SBX_LOG"
}

@test "sandbox: reconnect injects credentials + sidecar" {
  mkdir -p "$TMPDIR_TEST/sbx_reconnect/agent" "$TMPDIR_TEST/sbx_reconnect/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_reconnect/agent/allowlist.txt"

  security() { echo '{"oauth_token":"fake"}'; }
  export -f security
  sbx() {
    echo "sbx $*" >> "$SBX_LOG"
    if [ "$1" = "ls" ]; then echo "my-sandbox  running  2h"; return 0; fi
  }
  export -f sbx
  export OLA_ENV_BLOB='LLM_API_KEY="tok"'

  cd "$TMPDIR_TEST/sbx_reconnect/code"
  ola-sandbox my-sandbox

  grep -q 'sbx ls' "$SBX_LOG"
  grep -q 'sbx exec my-sandbox bash' "$SBX_LOG"
  grep -q '\.ola/agent.env' "$SBX_LOG"
  [[ "$(tail -1 "$SBX_LOG")" == *"sbx run my-sandbox"* ]]
}

@test "sandbox: reconnect re-patches agent_settings.json from resolved blob" {
  export HOME="$TMPDIR_TEST/rc_oh_home"
  mkdir -p "$HOME/.claude" "$HOME/.openhands"
  echo '{"oauth_token":"fake"}' > "$HOME/.claude/.credentials.json"
  _oh_template > "$HOME/.openhands/agent_settings.json"
  mkdir -p "$TMPDIR_TEST/rc_oh/agent" "$TMPDIR_TEST/rc_oh/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/rc_oh/agent/allowlist.txt"

  security() { echo '{"oauth_token":"fake"}'; }
  export -f security
  sbx() {
    echo "sbx $*" >> "$SBX_LOG"
    if [ "$1" = "ls" ]; then echo "rc-oh  running  1h"; return 0; fi
  }
  export -f sbx
  export OLA_ENV_BLOB='LLM_MODEL="openai/qwen3.5"
LLM_API_KEY="RKEY"
LLM_BASE_URL="https://10.9.8.7/v1"'

  cd "$TMPDIR_TEST/rc_oh/code"
  ola-sandbox rc-oh

  grep -q 'agent_settings.json' "$SBX_LOG"
  local out
  out="$(_decode_written agent_settings.json)"
  [ "$(echo "$out" | jq -r '.llm.base_url')" = "https://10.9.8.7/v1" ]
  [ "$(echo "$out" | jq -r '.condenser.llm.api_key')" = "RKEY" ]
}

_mock_sbx_new_sandbox() {
  security() { echo '{"oauth_token":"fake"}'; }
  export -f security
  # Deterministic Docker VM size so the computed 80% memory cap is stable:
  # 16748113920 bytes (15.6 GiB) → 80% = 12777 MiB → `-m 12777m`.
  docker() { [ "$1" = "info" ] && echo "16748113920"; }
  export -f docker
  eval "
  sbx() {
    echo \"sbx \$*\" >> \"$SBX_LOG\"
    if [ \"\$1\" = \"ls\" ]; then echo 'other-sandbox  running  1h'; return 0; fi
  }
  export -f sbx
  "
}

@test "sandbox: create new sandbox" {
  mkdir -p "$TMPDIR_TEST/sbx_new/agent" "$TMPDIR_TEST/sbx_new/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_new/agent/allowlist.txt"

  _mock_sbx_new_sandbox

  cd "$TMPDIR_TEST/sbx_new/code"
  ola-sandbox new-sandbox

  grep -q 'sbx ls' "$SBX_LOG"
  grep -q "sbx policy allow network docs.docker.com" "$SBX_LOG"
  grep -q "sbx create shell --name new-sandbox --template ghcr.io/$(whoami)/ola:latest -m 12777m -q" "$SBX_LOG"
  grep -q "sbx_new$" "$SBX_LOG"
  ! grep -q 'agent:ro' "$SBX_LOG"
  grep -q "sbx exec new-sandbox bash" "$SBX_LOG"
  grep -q 'sbx run new-sandbox' "$SBX_LOG"
}

@test "sandbox: OLA_SBX_IMAGE override" {
  mkdir -p "$TMPDIR_TEST/sbx_custom/agent" "$TMPDIR_TEST/sbx_custom/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_custom/agent/allowlist.txt"

  _mock_sbx_new_sandbox

  cd "$TMPDIR_TEST/sbx_custom/code"
  OLA_SBX_IMAGE="myregistry.io/custom:v2" ola-sandbox custom-sandbox

  grep -q '\--template myregistry.io/custom:v2' "$SBX_LOG"
  grep -q 'sbx create shell' "$SBX_LOG"
}

@test "sandbox: OLA_SBX_MEMORY override sets -m verbatim (bypasses cap)" {
  mkdir -p "$TMPDIR_TEST/sbx_mem/agent" "$TMPDIR_TEST/sbx_mem/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_mem/agent/allowlist.txt"

  _mock_sbx_new_sandbox

  cd "$TMPDIR_TEST/sbx_mem/code"
  OLA_SBX_MEMORY="24g" ola-sandbox mem-sandbox

  # Override wins over the computed 80% (12777m), passed through verbatim.
  grep -q "sbx create shell --name mem-sandbox --template ghcr.io/$(whoami)/ola:latest -m 24g -q" "$SBX_LOG"
}

@test "sandbox: omits -m when Docker VM size is unreadable (sbx default applies)" {
  mkdir -p "$TMPDIR_TEST/sbx_nomem/agent" "$TMPDIR_TEST/sbx_nomem/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_nomem/agent/allowlist.txt"

  security() { echo '{"oauth_token":"fake"}'; }
  export -f security
  # docker info fails → _ola_sbx_memory returns non-zero → no -m flag.
  docker() { return 1; }
  export -f docker
  sbx() {
    echo "sbx $*" >> "$SBX_LOG"
    if [ "$1" = "ls" ]; then echo 'other-sandbox  running  1h'; return 0; fi
  }
  export -f sbx

  cd "$TMPDIR_TEST/sbx_nomem/code"
  ola-sandbox nomem-sandbox

  grep -q "sbx create shell --name nomem-sandbox --template ghcr.io/$(whoami)/ola:latest -q" "$SBX_LOG"
  ! grep -qE 'sbx create .* -m ' "$SBX_LOG"
}

@test "sandbox: prefers local ola:dev image when present in template store" {
  mkdir -p "$TMPDIR_TEST/sbx_local/agent" "$TMPDIR_TEST/sbx_local/code"
  echo "docs.docker.com" > "$TMPDIR_TEST/sbx_local/agent/allowlist.txt"

  security() { echo '{"oauth_token":"fake"}'; }
  export -f security
  sbx() {
    echo "sbx $*" >> "$SBX_LOG"
    if [ "$1" = "ls" ]; then echo 'other-sandbox  running  1h'; return 0; fi
    if [ "$1" = "template" ] && [ "$2" = "ls" ]; then
      echo 'ola                    dev      4715041c5671   shell-docker   About an hour ago'
      return 0
    fi
  }
  export -f sbx

  cd "$TMPDIR_TEST/sbx_local/code"
  ola-sandbox local-sandbox

  grep -q '\--template ola:dev' "$SBX_LOG"
  ! grep -q 'ghcr.io' "$SBX_LOG"
}
