# ola shell helpers — cc-credentials, ola-sandbox, ola-policy-sync
# Symlink to ~/.ola.sh and source from .zshrc:
#   ln -sf /path/to/ola/ola.sh ~/.ola.sh
#   [ -f ~/.ola.sh ] && source ~/.ola.sh

# Resolve the real directory of this script (follows symlinks)
_OLA_DIR="${${(%):-%x}:A:h}"

# Restore ~/.claude/.credentials.json from macOS Keychain.
# Claude Code stores its OAuth token in the Keychain; this extracts it to a
# file so it can be copied into sandboxes.
cc-credentials() {
  local cred_file="$HOME/.claude/.credentials.json"
  local service="Claude Code-credentials"
  local account="$(whoami)"

  local data
  data="$(security find-generic-password -s "$service" -a "$account" -w 2>/dev/null)"
  if [ $? -ne 0 ] || [ -z "$data" ]; then
    echo "Error: no credentials found in Keychain (service=$service, account=$account)" >&2
    echo "Run 'claude' on the host first to authenticate via OAuth." >&2
    return 1
  fi

  mkdir -p "$HOME/.claude"
  printf '%s' "$data" > "$cred_file"
  chmod 600 "$cred_file"
  echo "Restored $cred_file from Keychain"
}

# Extract hostname from a URL string (strips scheme, port, path).
# Usage: _ola_host_from_url "https://example.com:8080/path" → "example.com"
_ola_host_from_url() {
  local url="$1"
  # Strip scheme (http:// or https://)
  local host="${url#*://}"
  # Strip path
  host="${host%%/*}"
  # Strip port
  host="${host%%:*}"
  echo "$host"
}

# Sync project-specific domains from allowlist.txt and .env into sbx network policy.
# Reads ../agent/allowlist.txt and .env (in code dir) for URL-valued variables.
# Adds each domain (plus wildcard subdomain) to the sbx balanced policy allowlist.
# Safe to run multiple times — sbx policy allow is idempotent.
ola-policy-sync() {
  local agent_dir="${1:-$(cd ../agent 2>/dev/null && pwd)}"
  local env_file="${2:-.env}"

  if [ -z "$agent_dir" ]; then
    echo "Error: agent directory not found. Pass path or run from project dir." >&2
    return 1
  fi

  local count=0

  # 1. Sync domains from allowlist.txt
  local allowlist="$agent_dir/allowlist.txt"
  if [ -f "$allowlist" ]; then
    while IFS= read -r host || [ -n "$host" ]; do
      [[ -z "$host" || "$host" == \#* ]] && continue
      sbx policy allow network "$host,*.$host" 2>/dev/null
      count=$((count + 1))
    done < "$allowlist"
  fi

  # 2. Source .env and sync hostnames from *_BASE_URL env vars
  if [ -f "$env_file" ]; then
    local _ola_env
    _ola_env="$(set -a; source "$env_file" 2>/dev/null; env)"

    local _ola_urls
    _ola_urls="$(echo "$_ola_env" | grep '_BASE_URL=' | while IFS='=' read -r key val; do echo "$val"; done)"
    local url host
    for url in $_ola_urls; do
      [[ "$url" == https://* || "$url" == http://* ]] || continue
      host="$(_ola_host_from_url "$url")"
      if [ -n "$host" ] && [ "$host" != "localhost" ] && [[ "$host" != 127.* ]]; then
        sbx policy allow network "$host,*.$host" 2>/dev/null
        count=$((count + 1))
      fi
    done

    # 3. Laminar: if API key is in .env, allow the base URL (or localhost:<port>)
    local _lmnr_key
    _lmnr_key="$(grep -E '^LMNR_PROJECT_API_KEY=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'")"
    if [ -n "$_lmnr_key" ]; then
      local _lmnr_base
      _lmnr_base="$(grep -E '^LMNR_BASE_URL=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'")"
      local _lmnr_host
      _lmnr_host="$(_ola_host_from_url "${_lmnr_base:-http://localhost}")"
      if [ "$_lmnr_host" = "localhost" ] || [[ "$_lmnr_host" == 127.* ]]; then
        local _lmnr_port
        _lmnr_port="$(grep -E '^LMNR_HTTP_PORT=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'")"
        sbx policy allow network "localhost:${_lmnr_port:-8000}" 2>/dev/null
        count=$((count + 1))
      else
        sbx policy allow network "$_lmnr_host,*.$_lmnr_host" 2>/dev/null
        count=$((count + 1))
      fi
    fi
  fi

  echo "Synced $count domain(s) to sbx policy."
}

# Review sbx network policy against project allowlist.
# Lists current balanced policy rules and checks for:
#   - Allowlist domains NOT yet covered by any policy rule
#   - Overly broad wildcards in the policy (for manual review)
# Usage: ola-policy-review [agent_dir]
ola-policy-review() {
  local agent_dir="${1:-$(cd ../agent 2>/dev/null && pwd)}"

  if [ -z "$agent_dir" ]; then
    echo "Error: agent directory not found. Pass path or run from project dir." >&2
    return 1
  fi

  # Capture current network policy rules
  local policy_output
  policy_output="$(sbx policy ls --type network 2>/dev/null)" || {
    echo "Error: failed to list sbx policies. Is sbx installed and running?" >&2
    return 1
  }

  echo "=== Current sbx network policy ==="
  echo "$policy_output"
  echo ""

  # Flag overly broad wildcards for manual review
  local broad_rules
  broad_rules="$(echo "$policy_output" | grep -E '\*\.[a-z]+\.[a-z]+$' || true)"
  if [ -n "$broad_rules" ]; then
    echo "=== Broad wildcards (review if needed) ==="
    echo "$broad_rules"
    echo ""
  fi

  # Check allowlist.txt domains against policy
  local allowlist="$agent_dir/allowlist.txt"
  if [ ! -f "$allowlist" ]; then
    echo "No allowlist.txt found at $allowlist"
    return 0
  fi

  local missing=0
  local covered=0
  echo "=== Allowlist domain coverage ==="
  while IFS= read -r host || [ -n "$host" ]; do
    [[ -z "$host" || "$host" == \#* ]] && continue
    if echo "$policy_output" | grep -qF "$host"; then
      echo "  [covered] $host"
      covered=$((covered + 1))
    else
      echo "  [MISSING] $host — run: sbx policy allow network \"$host,*.$host\""
      missing=$((missing + 1))
    fi
  done < "$allowlist"

  echo ""
  echo "Summary: $covered covered, $missing missing"
  [ "$missing" -eq 0 ] || return 1
}

# Copy a host file into a running sandbox via base64 encoding.
# Usage: _ola_inject_file <sandbox_name> <host_path> <sandbox_path>
_ola_inject_file() {
  local name="$1" src="$2" dest="$3"
  if [ ! -f "$src" ]; then
    return 1
  fi
  local dir="${dest%/*}"
  sbx exec "$name" bash -c "mkdir -p $dir" 2>/dev/null
  local data
  data="$(base64 < "$src")"
  sbx exec "$name" bash -c "echo '$data' | base64 -d > $dest" 2>/dev/null
}

# Inject agent credentials and config into a running sandbox.
_ola_inject_credentials() {
  local name="$1"

  # Claude Code: OAuth credentials from Keychain
  local cc_cred="$HOME/.claude/.credentials.json"
  if ! _ola_inject_file "$name" "$cc_cred" "\$HOME/.claude/.credentials.json"; then
    echo "Warning: $cc_cred not found — run 'cc-credentials' or 'claude' on the host first." >&2
  fi

  # OpenHands: agent settings and CLI config
  local oh_dir="$HOME/.openhands"
  _ola_inject_file "$name" "$oh_dir/agent_settings.json" "\$HOME/.openhands/agent_settings.json" || true
  _ola_inject_file "$name" "$oh_dir/cli_config.json" "\$HOME/.openhands/cli_config.json" || true
}

ola-sandbox() {
  local name="${1:?Usage: ola-sandbox <sandbox_name>}"
  local code_dir="$(pwd)"
  local code_name="$(basename "$code_dir")"
  local project_dir="$(cd .. && pwd)"
  local agent_dir="$(cd ../agent 2>/dev/null && pwd)"

  if [ -z "$agent_dir" ]; then
    echo "Error: ../agent directory not found relative to $(pwd)" >&2
    return 1
  fi

  # Extract fresh credentials from Keychain
  cc-credentials || true

  # Apply project-specific network allowlist (additive to default policy).
  # This applies immediately to all local sandboxes
  # If policy was already added, it has no effect
  ola-policy-sync "$agent_dir" "$agent_dir/.env"

  # Reconnect if sandbox already exists
  if sbx ls 2>/dev/null | grep -q "$name"; then
    # Refresh credentials on reconnect
    _ola_inject_credentials "$name"
    sbx run "$name"
    return
  fi

  # Create sandbox non-interactively, then attach.
  # The template extends docker/sandbox-templates:shell, so the agent is "shell".
  # sbx pulls templates from a registry (not the local Docker daemon), so the
  # image must be pushed to a registry first (see README).
  local image="${OLA_SBX_IMAGE:-ghcr.io/$(whoami)/ola:latest}"

  sbx create shell \
    --name "$name" \
    --template "$image" \
    -q \
    "$project_dir" || {
    echo "Error: failed to create sandbox '$name'" >&2
    return 1
  }

  _ola_inject_credentials "$name"

  # Set login shell to land in the src dir
  sbx exec "$name" bash -c \
    "echo 'cd $code_dir' >> ~/.bashrc" 2>/dev/null

  # Attach to the sandbox (foreground, interactive)
  sbx run "$name"
}
