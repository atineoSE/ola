# ola shell helpers — ola-sandbox
# Symlink to ~/.ola.sh and source from .zshrc:
#   ln -sf /path/to/ola/ola.sh ~/.ola.sh
#   [ -f ~/.ola.sh ] && source ~/.ola.sh

# Resolve the real directory of this script (follows symlinks)
_OLA_DIR="${${(%):-%x}:A:h}"

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

  # Apply project-specific network allowlist (additive to balanced policy)
  local whitelist="$agent_dir/whitelist.txt"
  if [ -f "$whitelist" ]; then
    while IFS= read -r host || [ -n "$host" ]; do
      [ -z "$host" ] && continue
      sbx policy allow network "$host,*.$host" 2>/dev/null
    done < "$whitelist"
  fi

  # Create and run with custom template + read-only agent mount
  # sbx handles proxy, credentials (via sbx secret), and network policy (balanced mode)
  sbx run claude \
    --name "$name" \
    --template docker.io/ola/ola-sbx:latest \
    "$code_dir" "$agent_dir:ro"
}
