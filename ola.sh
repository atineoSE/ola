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

# Add a global sbx network allow rule. As of sbx v0.29.0 the command
# requires a scope (`-g/--global` or a SANDBOX) before RESOURCES; the
# bare `sbx policy allow network RESOURCES` form now exits non-zero.
# Failures are surfaced rather than swallowed: a discarded error here
# once let that breaking CLI change disable policy sync for every run
# with no signal (sync still printed "Synced N" while adding nothing).
# Idempotent: re-adding a covered rule is a no-op and exits 0.
_ola_policy_allow() {
  local resources="$1" out
  if ! out="$(sbx policy allow network -g "$resources" 2>&1)"; then
    echo "Error: 'sbx policy allow network -g $resources' failed: $out" >&2
    return 1
  fi
  return 0
}

# Allow a resolved endpoint host in the sbx network policy. A bare IPv4
# literal must NOT get a `*.<ip>` wildcard appended — that is not a valid
# host pattern and sbx can reject the whole rule, which is exactly what
# silently blocked the LLM endpoint before.
_ola_allow_host() {
  local h="$1"
  [ -z "$h" ] && return 0
  if printf '%s' "$h" | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
    _ola_policy_allow "$h"
  else
    _ola_policy_allow "$h,*.$h"
  fi
}

# Extract a resolved value from an `ola env` blob (KEY="VALUE" lines).
# Reverses the double-quote/backslash escaping from
# envresolve.format_sidecar. Endpoint values (URLs, ports) are simple.
_ola_blob_val() {
  local blob="$1" key="$2" line
  line="$(printf '%s\n' "$blob" | grep -E "^${key}=" | head -1)"
  [ -z "$line" ] && return 0
  line="${line#${key}=\"}"
  line="${line%\"}"
  line="${line//\\\"/\"}"
  line="${line//\\\\/\\}"
  printf '%s' "$line"
}

# Apply the sbx network policy from allowlist.txt plus the LLM/LMNR
# endpoints in a resolved `ola env` blob. Idempotent (re-adding a covered
# rule is a no-op, exit 0). Returns non-zero if ANY rule failed to apply,
# so the caller can abort: a sandbox whose network policy is missing the
# LLM endpoint will run, then hard-fail the instant the agent calls the
# (blocked) model — which is strictly worse than refusing to start.
_ola_apply_policy() {
  local agent_dir="$1" blob="$2" count=0 failed=0

  local allowlist="$agent_dir/allowlist.txt"
  if [ -f "$allowlist" ]; then
    while IFS= read -r host || [ -n "$host" ]; do
      [[ -z "$host" || "$host" == \#* ]] && continue
      if _ola_policy_allow "$host,*.$host"; then
        count=$((count + 1))
      else
        failed=$((failed + 1))
      fi
    done < "$allowlist"
  fi

  local _llm_base _llm_host _llm_port
  _llm_base="$(_ola_blob_val "$blob" LLM_BASE_URL)"
  if [ -n "$_llm_base" ]; then
    _llm_host="$(_ola_host_from_url "$_llm_base")"
    _llm_port="$(_ola_port_from_url "$_llm_base")"
    if [ "$_llm_host" = "localhost" ] || [[ "$_llm_host" == 127.* ]]; then
      if [ -n "$_llm_port" ]; then
        if _ola_policy_allow "localhost:$_llm_port"; then
          count=$((count + 1))
        else
          failed=$((failed + 1))
        fi
      fi
    elif [ -n "$_llm_host" ]; then
      if _ola_allow_host "$_llm_host"; then
        count=$((count + 1))
      else
        failed=$((failed + 1))
      fi
    fi
  fi

  local _lmnr_base _lmnr_host _lmnr_port
  _lmnr_base="$(_ola_blob_val "$blob" LMNR_BASE_URL)"
  if [ -n "$_lmnr_base" ]; then
    _lmnr_host="$(_ola_host_from_url "$_lmnr_base")"
    if [ "$_lmnr_host" = "localhost" ] || [[ "$_lmnr_host" == 127.* ]]; then
      _lmnr_port="$(_ola_blob_val "$blob" LMNR_HTTP_PORT)"
      if [ -n "$_lmnr_port" ]; then
        if _ola_policy_allow "localhost:$_lmnr_port"; then
          count=$((count + 1))
        else
          failed=$((failed + 1))
        fi
      fi
    else
      if _ola_allow_host "$_lmnr_host"; then
        count=$((count + 1))
      else
        failed=$((failed + 1))
      fi
    fi
  fi

  if [ "$failed" -gt 0 ]; then
    echo "Error: $failed sbx policy rule(s) failed to apply ($count succeeded)." >&2
    return 1
  fi
  echo "Synced $count domain(s) to sbx policy."
  return 0
}

# Write the host-resolved env snapshot into the sandbox so the in-sandbox
# `ola` loads concrete values (no ${VAR} interpolation needed there).
# Path mirrors python: ola.sandbox.SIDECAR_ENV = ~/.ola/agent.env
_ola_inject_sidecar() {
  local name="$1" blob="$2"
  [ -z "$blob" ] && return 0
  local data
  data="$(printf '%s\n' "$blob" | base64)"
  sbx exec "$name" bash -c 'mkdir -p "$HOME/.ola"' 2>/dev/null
  sbx exec "$name" bash -c "echo '$data' | base64 -d > \$HOME/.ola/agent.env" 2>/dev/null
}

# Sync the sbx network policy from the two project config files:
#   - agent/allowlist.txt : static domains
#   - agent/.env          : LLM + Laminar endpoints (resolved by `ola env`,
#                           which fails fast if a mandatory host var is unset)
# Safe to run multiple times — sbx policy allow is idempotent.
ola-policy-sync() {
  local agent_dir="${1:-$(cd ../agent 2>/dev/null && pwd)}"

  if [ -z "$agent_dir" ]; then
    echo "Error: agent directory not found. Pass path or run from project dir." >&2
    return 1
  fi

  # `ola env` validates host-sourced ${VAR} refs and prints the resolved
  # snapshot; a non-zero exit means the host environment is not sound.
  local blob
  blob="$(ola env -f "$agent_dir")" || return 1
  _ola_apply_policy "$agent_dir" "$blob" || return 1
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
      echo "  [MISSING] $host — run: sbx policy allow network -g \"$host,*.$host\""
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

  # OpenHands: agent settings and CLI config. agent_settings.json is the
  # baseline copy; _ola_inject_oh_settings overwrites it with the resolved
  # endpoint (the host copy carries a stale, possibly-rotated base_url).
  local oh_dir="$HOME/.openhands"
  _ola_inject_file "$name" "$oh_dir/agent_settings.json" "\$HOME/.openhands/agent_settings.json" || true
  _ola_inject_file "$name" "$oh_dir/cli_config.json" "\$HOME/.openhands/cli_config.json" || true
}

# Patch the host OpenHands CLI settings with the resolved LLM endpoint and
# inject them into the sandbox. ola's OpenHands *SDK* path reads LLM_* from
# the environment (it ignores agent_settings.json by design), but the
# standalone `openhands` CLI inside the sandbox reads agent_settings.json —
# whose host copy carries a stale base_url/key (the substrate IP rotates).
# Only the rotating identity fields are patched; the user's tools/condenser/
# prompt config in the template is preserved verbatim. Non-fatal: on any
# gap (no jq, no template, missing values) the verbatim host copy from
# _ola_inject_credentials plus the exported shell env remain as fallback.
_ola_inject_oh_settings() {
  local name="$1" blob="$2"
  local tmpl="$HOME/.openhands/agent_settings.json"
  [ -f "$tmpl" ] || return 0
  if ! command -v jq >/dev/null 2>&1; then
    echo "Warning: jq not found; sandbox openhands CLI keeps the stale host agent_settings.json." >&2
    return 0
  fi

  local _m _k _u
  _m="$(_ola_blob_val "$blob" LLM_MODEL)"
  _k="$(_ola_blob_val "$blob" LLM_API_KEY)"
  _u="$(_ola_blob_val "$blob" LLM_BASE_URL)"
  if [ -z "$_m" ] || [ -z "$_k" ] || [ -z "$_u" ]; then
    return 0
  fi

  local patched
  patched="$(jq --arg m "$_m" --arg k "$_k" --arg u "$_u" '
    .llm.model = $m | .llm.api_key = $k | .llm.base_url = $u
    | if (.condenser.llm? // null) != null then
        .condenser.llm.model = $m
        | .condenser.llm.api_key = $k
        | .condenser.llm.base_url = $u
      else . end
  ' "$tmpl" 2>/dev/null)" || {
    echo "Warning: failed to patch agent_settings.json; sandbox openhands CLI keeps the stale host copy." >&2
    return 0
  }

  local data
  data="$(printf '%s' "$patched" | base64)"
  sbx exec "$name" bash -c 'mkdir -p "$HOME/.openhands"' 2>/dev/null
  sbx exec "$name" bash -c \
    "echo '$data' | base64 -d > \$HOME/.openhands/agent_settings.json" 2>/dev/null
}

# Set up the sandbox login shell: export the resolved env snapshot so every
# tool in the sandbox (the openhands CLI, manual litellm/curl probes, codex)
# sees the same LLM_* values ola's SDK loads — they otherwise live only in
# ~/.ola/agent.env, which only ola's Python reads. Mirrors the SDK's
# LLM_SKIP_TLS_VERIFY → SSL_VERIFY translation (see agents/openhands.py) so
# self-signed substrate certs work for env-driven tools too. Sourced by path
# so a reconnect that refreshes the sidecar is picked up with no rc rewrite.
_ola_setup_shell_rc() {
  local name="$1" code_dir="$2" rc data
  rc="$(cat <<EOF
set -a
[ -f "\$HOME/.ola/agent.env" ] && . "\$HOME/.ola/agent.env"
set +a
[ "\${LLM_SKIP_TLS_VERIFY:-}" = "true" ] && export SSL_VERIFY=False
cd $code_dir
EOF
)"
  data="$(printf '%s\n' "$rc" | base64)"
  sbx exec "$name" bash -c \
    "echo '$data' | base64 -d >> \$HOME/.bashrc" 2>/dev/null
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

  # Resolve & validate the agent .env on the host BEFORE touching sbx.
  # Fail-fast: the host environment must be sound (every mandatory ${VAR}
  # set) before we create or reconnect a sandbox. `ola env` prints the
  # reason on stderr; we just abort. Re-evaluated on every create AND
  # reconnect, symmetric with allowlist.txt.
  local _env_blob
  _env_blob="$(ola env -f "$agent_dir")" || {
    echo "Error: agent .env validation failed; not creating/reconnecting '$name'." >&2
    return 1
  }

  # Apply project-specific network policy (allowlist.txt + resolved
  # endpoints). Additive + idempotent across all local sandboxes. Abort
  # on failure: starting a sandbox whose policy is missing the LLM
  # endpoint only defers the failure to the first model call, with a
  # confusing "blocked by network policy" deep in the agent logs.
  _ola_apply_policy "$agent_dir" "$_env_blob" || {
    echo "Error: sbx network policy sync failed; not creating/reconnecting '$name'." >&2
    return 1
  }

  # Reconnect if sandbox already exists
  if sbx ls 2>&1 | grep -q "$name"; then
    # Refresh credentials and the resolved env snapshot on reconnect.
    # agent_settings.json is re-patched too: the substrate endpoint may
    # have rotated since the sandbox was created, symmetric with the
    # sidecar. The shell rc persists in container state, so it is not
    # rewritten here (it sources the refreshed sidecar by path).
    _ola_inject_credentials "$name"
    _ola_inject_sidecar "$name" "$_env_blob"
    _ola_inject_oh_settings "$name" "$_env_blob"
    sbx run "$name"
    return
  fi

  # Create sandbox non-interactively, then attach.
  # Image precedence: explicit OLA_SBX_IMAGE override, else the local dev
  # image (ola:dev) if 'make sandbox-dev' loaded it into sbx's template
  # store, else the registry image pulled on demand.
  local image="$OLA_SBX_IMAGE"
  if [ -z "$image" ]; then
    if sbx template ls 2>/dev/null | grep -qE '^ola[[:space:]]+dev[[:space:]]'; then
      image="ola:dev"
    else
      image="ghcr.io/$(whoami)/ola:latest"
    fi
  fi

  sbx create shell \
    --name "$name" \
    --template "$image" \
    -q \
    "$project_dir" || {
    echo "Error: failed to create sandbox '$name'" >&2
    return 1
  }

  _ola_inject_credentials "$name"
  _ola_inject_sidecar "$name" "$_env_blob"
  _ola_inject_oh_settings "$name" "$_env_blob"

  # Export the resolved env into the login shell and land in the project repo.
  _ola_setup_shell_rc "$name" "$code_dir"

  # Attach to the sandbox (foreground, interactive)
  sbx run "$name"
}
