# ola shell helpers — ola-sandbox, ola-policy-sync
# Symlink to ~/.ola.sh and source from .zshrc:
#   ln -sf /path/to/ola/ola.sh ~/.ola.sh
#   [ -f ~/.ola.sh ] && source ~/.ola.sh

# Resolve the real directory of this script (follows symlinks)
_OLA_DIR="${${(%):-%x}:A:h}"

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

# Sync project-specific domains from whitelist.txt and .env into sbx network policy.
# Reads ../agent/whitelist.txt and .env (in code dir) for URL-valued variables.
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

  # 1. Sync domains from whitelist.txt
  local whitelist="$agent_dir/whitelist.txt"
  if [ -f "$whitelist" ]; then
    while IFS= read -r host || [ -n "$host" ]; do
      [[ -z "$host" || "$host" == \#* ]] && continue
      sbx policy allow network "$host,*.$host" 2>/dev/null
      count=$((count + 1))
    done < "$whitelist"
  fi

  # 2. Sync hostnames from *_BASE_URL variables in .env
  if [ -f "$env_file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      # Match VAR_BASE_URL=value (with or without quotes)
      if [[ "$line" =~ ^[A-Z_]*_BASE_URL=[\"\']?(https?://[^\"\']*)[\"\']?$ ]]; then
        local url="${BASH_REMATCH[1]}"
        local host
        host="$(_ola_host_from_url "$url")"
        if [ -n "$host" ] && [ "$host" != "localhost" ] && [[ "$host" != 127.* ]]; then
          sbx policy allow network "$host,*.$host" 2>/dev/null
          count=$((count + 1))
        fi
      fi
    done < "$env_file"
  fi

  echo "Synced $count domain(s) to sbx policy."
}

# Review sbx network policy against project whitelist.
# Lists current balanced policy rules and checks for:
#   - Whitelist domains NOT yet covered by any policy rule
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

  # Check whitelist.txt domains against policy
  local whitelist="$agent_dir/whitelist.txt"
  if [ ! -f "$whitelist" ]; then
    echo "No whitelist.txt found at $whitelist"
    return 0
  fi

  local missing=0
  local covered=0
  echo "=== Whitelist domain coverage ==="
  while IFS= read -r host || [ -n "$host" ]; do
    [[ -z "$host" || "$host" == \#* ]] && continue
    if echo "$policy_output" | grep -qF "$host"; then
      echo "  [covered] $host"
      covered=$((covered + 1))
    else
      echo "  [MISSING] $host — run: sbx policy allow network \"$host,*.$host\""
      missing=$((missing + 1))
    fi
  done < "$whitelist"

  echo ""
  echo "Summary: $covered covered, $missing missing"
  [ "$missing" -eq 0 ] || return 1
}

# Copy host ~/.claude/.credentials.json into a running sandbox.
# Usage: _ola_inject_credentials <sandbox_name> <host_credentials_path>
_ola_inject_credentials() {
  local name="$1" cred_src="$2"
  if [ ! -f "$cred_src" ]; then
    echo "Warning: $cred_src not found — Claude auth may fail inside sandbox." >&2
    echo "Run 'claude' on the host first to authenticate, then re-run ola-sandbox." >&2
    return 1
  fi
  sbx exec "$name" bash -c 'mkdir -p ~/.claude' 2>/dev/null
  sbx cp "$cred_src" "$name:/home/user/.claude/.credentials.json" 2>/dev/null || {
    echo "Warning: failed to copy credentials into sandbox." >&2
    return 1
  }
}

ola-sandbox() {
  local name="${1:?Usage: ola-sandbox <sandbox_name>}"
  local code_dir="$(pwd)"
  local agent_dir="$(cd ../agent 2>/dev/null && pwd)"

  if [ -z "$agent_dir" ]; then
    echo "Error: ../agent directory not found relative to $(pwd)" >&2
    return 1
  fi

  local cred_src="$HOME/.claude/.credentials.json"

  # Reconnect if sandbox already exists
  if sbx ls 2>/dev/null | grep -q "$name"; then
    # Refresh credentials on reconnect
    _ola_inject_credentials "$name" "$cred_src"
    sbx run claude --name "$name"
    return
  fi

  # Ensure balanced network policy is active (deny-all + common dev allowlist)
  sbx policy set-default balanced

  # Apply project-specific network allowlist (additive to balanced policy)
  ola-policy-sync "$agent_dir"

  # Create and run with custom template + read-only agent mount
  # sbx handles proxy and network policy (balanced mode)
  # Credentials are copied in after sandbox starts (OAuth token from host)
  sbx run claude \
    --name "$name" \
    --template "${OLA_SBX_IMAGE:-ola/ola:latest}" \
    "$code_dir" "$agent_dir:ro" &
  local sbx_pid=$!

  # Wait for sandbox to be ready, then inject credentials
  local elapsed=0
  while ! sbx ls 2>/dev/null | grep -q "$name"; do
    if [ $elapsed -ge 60 ]; then
      echo "Warning: sandbox not ready after 60s, skipping credential injection" >&2
      break
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  _ola_inject_credentials "$name" "$cred_src"

  # Bring sbx run back to foreground
  wait "$sbx_pid"
}
