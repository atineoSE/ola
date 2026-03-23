# ola shell helpers — cc-credentials and ola-sandbox
# Symlink to ~/.ola.sh and source from .zshrc:
#   ln -sf /path/to/ola/ola.sh ~/.ola.sh
#   [ -f ~/.ola.sh ] && source ~/.ola.sh

cc-credentials() {
  # Restore ~/.claude/.credentials.json from macOS Keychain
  local cred_file="$HOME/.claude/.credentials.json"
  local service="Claude Code-credentials"
  local account="$(whoami)"

  local data
  data="$(security find-generic-password -s "$service" -a "$account" -w 2>/dev/null)"
  if [ $? -ne 0 ] || [ -z "$data" ]; then
    echo "Error: no credentials found in Keychain (service=$service, account=$account)" >&2
    return 1
  fi

  mkdir -p "$HOME/.claude"
  printf '%s' "$data" > "$cred_file"
  chmod 600 "$cred_file"
  echo "Restored $cred_file from Keychain"
}

ola-sandbox() {
  local name="${1:?Usage: ola-sandbox <sandbox_name>}"

  # If sandbox already exists, just reconnect
  if docker sandbox list 2>/dev/null | grep -q "\\b${name}\\b"; then
    docker sandbox run "$name"
    return
  fi

  local code_dir="$(pwd)"
  local agent_dir="$(cd ../agent 2>/dev/null && pwd)"

  if [ -z "$agent_dir" ]; then
    echo "Error: ../agent directory not found relative to $(pwd)" >&2
    return 1
  fi

  # Ensure credentials exist, restoring from Keychain if needed
  if [ ! -f ~/.claude/.credentials.json ]; then
    echo "Credentials file missing, restoring from Keychain..."
    cc-credentials || return 1
  fi

  # Copy credentials and config into workspace for the sandbox to pick up
  cp ~/.claude/.credentials.json "$code_dir/.credentials.json"
  for f in agent_settings.json cli_config.json; do
    [ -f ~/.openhands/"$f" ] && cp ~/.openhands/"$f" "$code_dir/.oh-$f"
  done

  # Create the sandbox
  docker sandbox create --name "$name" -t ola:latest shell "$code_dir" "$agent_dir"

  # Apply network policy: deny all, allow only HTTPS on approved domains
  local -a net=(docker sandbox network proxy "$name" --policy deny)
  # Claude / Anthropic
  net+=(--allow-host api.anthropic.com:443)
  net+=(--allow-host claude.ai:443 --allow-host "*.claude.ai:443")
  # Package managers
  net+=(--allow-host "*.npmjs.org:443")
  net+=(--allow-host "*.pypi.org:443" --allow-host files.pythonhosted.org:443)
  net+=(--allow-host "*.rubygems.org:443")
  net+=(--allow-host deb.nodesource.com:443)
  # Allow additional LLM host (e.g. OpenHands proxy) via .env
  local env_file="$code_dir/../.env"
  if [ -f "$env_file" ]; then
    local base_url llm_host
    base_url="$(grep '^LLM_BASE_URL=' "$env_file" | cut -d= -f2 | tr -d '"'"'")"
    llm_host="${base_url#https://}"
    llm_host="${llm_host#http://}"
    llm_host="${llm_host%%/*}"
    [ -n "$llm_host" ] && net+=(--allow-host "$llm_host:443")
  fi
  "${net[@]}"

  # Run the sandbox
  docker sandbox run "$name"

  # Clean up credentials and config from workspace if still present
  rm -f "$code_dir/.credentials.json" "$code_dir/.oh-agent_settings.json" "$code_dir/.oh-cli_config.json"
}
