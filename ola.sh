# ola shell helpers — cc-credentials and ola-sandbox
# Symlink to ~/.ola.sh and source from .zshrc:
#   ln -sf /path/to/ola/ola.sh ~/.ola.sh
#   [ -f ~/.ola.sh ] && source ~/.ola.sh

# Resolve the real directory of this script (follows symlinks)
_OLA_DIR="${${(%):-%x}:A:h}"

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
  if docker sandbox list 2>/dev/null | awk '{print $1}' | grep -qx "$name"; then
    docker sandbox run "$name"
    return
  fi

  local code_dir="$(pwd)"
  local agent_dir="$(cd ../agent 2>/dev/null && pwd)"

  if [ -z "$agent_dir" ]; then
    echo "Error: ../agent directory not found relative to $(pwd)" >&2
    return 1
  fi

  # Always refresh credentials from Keychain before creating the sandbox
  cc-credentials || return 1

  # Copy credentials, config, and secrets into workspace for the sandbox to pick up
  # Use a trap to guarantee cleanup even on ctrl-C or failure
  _ola_cleanup() {
    rm -f "$code_dir/.credentials.json" "$code_dir/.oh-agent_settings.json" \
          "$code_dir/.oh-cli_config.json" "$code_dir/.ola-env"
  }
  trap _ola_cleanup EXIT INT TERM

  cp ~/.claude/.credentials.json "$code_dir/.credentials.json"
  for f in agent_settings.json cli_config.json; do
    [ -f ~/.openhands/"$f" ] && \
      sed 's|://localhost|://host.docker.internal|g; s|://127\.0\.0\.1|://host.docker.internal|g' \
        ~/.openhands/"$f" > "$code_dir/.oh-$f"
  done
  if [ -f "$_OLA_DIR/.env" ]; then
    # Remap localhost → host.docker.internal so services on the host
    # are reachable from inside the Docker sandbox.
    sed 's|://localhost|://host.docker.internal|g; s|://127\.0\.0\.1|://host.docker.internal|g' \
      "$_OLA_DIR/.env" > "$code_dir/.ola-env"
  fi

  # Create the sandbox
  docker sandbox create --name "$name" -t ola:latest shell "$code_dir" "$agent_dir"

  # Apply network policy: deny all, allow only HTTPS on approved domains
  local -a net=(docker sandbox network proxy "$name" --policy deny)

  # _allow_host <host[:port]> — adds both the exact host and *.host so all
  # subdomains are reachable without having to list them individually.
  _allow_host() {
    local entry="$1"
    local host port
    if [[ "$entry" == *:* ]]; then
      port="${entry##*:}"
      host="${entry%%:*}"
    else
      port=443
      host="$entry"
    fi
    net+=(--allow-host "$host:$port" --allow-host "*.$host:$port")
  }

  # Claude / Anthropic
  _allow_host api.anthropic.com:443
  _allow_host claude.ai:443
  # Package managers
  _allow_host npmjs.org:443
  _allow_host pypi.org:443
  _allow_host files.pythonhosted.org:443
  _allow_host rubygems.org:443
  _allow_host deb.nodesource.com:443
  # Allow additional hosts from .env
  local env_file="$_OLA_DIR/.env"
  if [ -f "$env_file" ]; then
    local base_url llm_host llm_port
    base_url="$(grep '^LLM_BASE_URL=' "$env_file" | cut -d= -f2 | tr -d '"'"'")"
    llm_host="${base_url#https://}"
    llm_host="${llm_host#http://}"
    llm_host="${llm_host%%/*}"
    if [[ "$llm_host" == *:* ]]; then
      llm_port="${llm_host##*:}"
      llm_host="${llm_host%%:*}"
    else
      llm_port=443
    fi
    [ -n "$llm_host" ] && _allow_host "$llm_host:$llm_port"

    local lmnr_base lmnr_host lmnr_http_port
    lmnr_base="$(grep '^LMNR_BASE_URL=' "$env_file" | cut -d= -f2 | tr -d '"'"'")"
    lmnr_host="${lmnr_base#https://}"
    lmnr_host="${lmnr_host#http://}"
    lmnr_host="${lmnr_host%%/*}"
    lmnr_http_port="$(grep '^LMNR_HTTP_PORT=' "$env_file" | cut -d= -f2 | tr -d '"'"'")"
    : "${lmnr_http_port:=8000}"
    [ -n "$lmnr_host" ] && _allow_host "$lmnr_host:$lmnr_http_port"
  fi
  # Allow additional hosts from agent whitelist file
  local whitelist="$agent_dir/whitelist.txt"
  if [ -f "$whitelist" ]; then
    while IFS= read -r line; do
      line="${line%%#*}"        # strip inline comments
      line="${line// /}"        # strip spaces
      [ -z "$line" ] && continue
      _allow_host "$line"
    done < "$whitelist"
  fi
  "${net[@]}"

  # Run the sandbox (trap handles cleanup on exit)
  docker sandbox run "$name"
}
