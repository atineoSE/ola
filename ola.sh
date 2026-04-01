# ola shell helpers — ola-sandbox, ola-policy-sync
# Symlink to ~/.ola.sh and source from .zshrc:
#   ln -sf /path/to/ola/ola.sh ~/.ola.sh
#   [ -f ~/.ola.sh ] && source ~/.ola.sh

# Resolve the real directory of this script (follows symlinks)
_OLA_DIR="${${(%):-%x}:A:h}"

# Sync project-specific domains from whitelist.txt into sbx network policy.
# Reads ../agent/whitelist.txt (relative to cwd) and adds each domain
# (plus wildcard subdomain) to the sbx balanced policy allowlist.
# Safe to run multiple times — sbx policy allow is idempotent.
ola-policy-sync() {
  local agent_dir="${1:-$(cd ../agent 2>/dev/null && pwd)}"

  if [ -z "$agent_dir" ]; then
    echo "Error: agent directory not found. Pass path or run from project dir." >&2
    return 1
  fi

  local whitelist="$agent_dir/whitelist.txt"
  if [ ! -f "$whitelist" ]; then
    echo "No whitelist found at $whitelist — nothing to sync." >&2
    return 0
  fi

  local count=0
  while IFS= read -r host || [ -n "$host" ]; do
    # Skip blank lines and comments
    [[ -z "$host" || "$host" == \#* ]] && continue
    sbx policy allow network "$host,*.$host" 2>/dev/null
    count=$((count + 1))
  done < "$whitelist"
  echo "Synced $count domain(s) from $whitelist to sbx policy."
}

ola-sandbox() {
  local name="${1:?Usage: ola-sandbox <sandbox_name>}"
  local code_dir="$(pwd)"
  local agent_dir="$(cd ../agent 2>/dev/null && pwd)"

  if [ -z "$agent_dir" ]; then
    echo "Error: ../agent directory not found relative to $(pwd)" >&2
    return 1
  fi

  # Reconnect if sandbox already exists
  if sbx ls 2>/dev/null | grep -q "$name"; then
    sbx run claude --name "$name"
    return
  fi

  # Ensure balanced network policy is active (deny-all + common dev allowlist)
  sbx policy set-default balanced

  # Apply project-specific network allowlist (additive to balanced policy)
  ola-policy-sync "$agent_dir"

  # Create and run with custom template + read-only agent mount
  # sbx handles proxy, credentials (via sbx secret), and network policy (balanced mode)
  sbx run claude \
    --name "$name" \
    --template docker.io/ola/ola-sbx:latest \
    "$code_dir" "$agent_dir:ro"
}
