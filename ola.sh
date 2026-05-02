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

# Extract port from a URL string (empty if no explicit port).
# Usage: _ola_port_from_url "https://example.com:8080/path" → "8080"
_ola_port_from_url() {
  local url="$1"
  local hostport="${url#*://}"
  hostport="${hostport%%/*}"
  case "$hostport" in
    *:*) echo "${hostport##*:}" ;;
    *)   echo "" ;;
  esac
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

  # 2. LLM_BASE_URL: allow the LLM endpoint (no action if missing)
  if [ -f "$env_file" ]; then
    local _llm_base
    _llm_base="$(grep -E '^LLM_BASE_URL=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'")"
    if [ -n "$_llm_base" ]; then
      local _llm_host _llm_port
      _llm_host="$(_ola_host_from_url "$_llm_base")"
      _llm_port="$(_ola_port_from_url "$_llm_base")"
      if [ "$_llm_host" = "localhost" ] || [[ "$_llm_host" == 127.* ]]; then
        if [ -n "$_llm_port" ]; then
          sbx policy allow network "localhost:$_llm_port" 2>/dev/null
          count=$((count + 1))
        fi
      elif [ -n "$_llm_host" ]; then
        sbx policy allow network "$_llm_host,*.$_llm_host" 2>/dev/null
        count=$((count + 1))
      fi
    fi

    # 3. LMNR_BASE_URL / LMNR_HTTP_PORT: allow Laminar endpoint (no action if missing)
    local _lmnr_base
    _lmnr_base="$(grep -E '^LMNR_BASE_URL=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'")"
    if [ -n "$_lmnr_base" ]; then
      local _lmnr_host
      _lmnr_host="$(_ola_host_from_url "$_lmnr_base")"
      if [ "$_lmnr_host" = "localhost" ] || [[ "$_lmnr_host" == 127.* ]]; then
        local _lmnr_port
        _lmnr_port="$(grep -E '^LMNR_HTTP_PORT=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'")"
        if [ -n "$_lmnr_port" ]; then
          sbx policy allow network "localhost:$_lmnr_port" 2>/dev/null
          count=$((count + 1))
        fi
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

  # Claude Code: OAuth credentials, user config, and settings
  local cc_dir="$HOME/.claude"
  local cc_cred="$cc_dir/.credentials.json"
  if ! _ola_inject_file "$name" "$cc_cred" "\$HOME/.claude/.credentials.json"; then
    echo "Warning: $cc_cred not found — run 'cc-credentials' or 'claude' on the host first." >&2
  fi
  _ola_inject_file "$name" "$cc_dir/.claude.json" "\$HOME/.claude/.claude.json" || true
  _ola_inject_file "$name" "$cc_dir/settings.json" "\$HOME/.claude/settings.json" || true

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

  # Fail fast if sbx is not authenticated — unauthenticated sbx commands stall.
  local _sbx_out
  _sbx_out="$(sbx ls 2>&1)"
  if [ $? -ne 0 ]; then
    echo "Error: sbx is not authenticated or unavailable." >&2
    echo "$_sbx_out" >&2
    echo "Run 'sbx login' and ensure Docker Desktop is running, then retry." >&2
    return 1
  fi

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
  if sbx ls 2>&1 | grep -q "$name"; then
    # Refresh credentials on reconnect
    _ola_inject_credentials "$name"
    sbx run "$name"
    return
  fi

  # Create sandbox non-interactively, then attach.
  # Default image is pulled from the registry. Set OLA_SBX_IMAGE to a local
  # tag (no registry host) to load from the local Docker daemon instead —
  # useful during ola development (see: make sandbox-dev).
  local image="${OLA_SBX_IMAGE:-ghcr.io/$(whoami)/ola:latest}"
  local create_flags=(-q)
  if [[ "$image" != *"/"* ]]; then
    # No registry host — load from local Docker daemon
    create_flags+=(--load-local-template)
  fi

  sbx create shell \
    --name "$name" \
    --template "$image" \
    "${create_flags[@]}" \
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
