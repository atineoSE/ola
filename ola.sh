# ola shell helpers — cc-credentials, ola-sandbox, ola-policy-sync
# Symlink to ~/.ola.sh and source from .zshrc:
#   ln -sf /path/to/ola/ola.sh ~/.ola.sh
#   [ -f ~/.ola.sh ] && source ~/.ola.sh

# Resolve the real directory of this script (follows symlinks)
_OLA_DIR="${${(%):-%x}:A:h}"

# Restore ~/.claude/.credentials.json from macOS Keychain.
# Claude Code stores its OAuth token in the Keychain; this extracts it to a
# file so it can be copied into sandboxes.
# Drop expired per-CLAUDE_CONFIG_DIR Keychain entries.
#
# Claude Code caches OAuth credentials per config dir in the Keychain under
# "Claude Code-credentials-<sha256(CLAUDE_CONFIG_DIR)[:8]>", and that entry
# OUTRANKS the .credentials.json file inside the same dir. ola's per-task
# config dirs are derived from the task id, so they are stable across runs:
# a run whose token expired mid-flight leaves a dead entry behind that
# poisons that task for every future run — the file cc-credentials refreshes
# is never consulted, and the run fails locally in ~40ms with
# "OAuth session expired and could not be refreshed", no API call made.
#
# Only *expired* entries are dropped, so a live session under some other
# CLAUDE_CONFIG_DIR is left alone. Claude Code recreates an entry from the
# file on demand, so removal is non-destructive. Host-only by construction:
# `security` exists solely on macOS, and inside the sandbox the file is
# already the only credential source.
_cc_clear_stale_keychain_entries() {
  local now_ms svc exp cleared=0 entries
  now_ms=$(( $(date +%s) * 1000 ))

  # The default "Claude Code-credentials" entry has no -<hash> suffix and so
  # never matches — it is the source of truth and must survive.
  entries="$(security dump-keychain 2>/dev/null \
    | sed -n 's/.*"svce"<blob>="\(Claude Code-credentials-[^"]*\)".*/\1/p' \
    | sort -u)"
  [ -z "$entries" ] && return 0

  while IFS= read -r svc; do
    [ -z "$svc" ] && continue
    # Flat-JSON field extraction, no jq dependency (mirrors _ola_blob_val).
    exp="$(security find-generic-password -s "$svc" -w 2>/dev/null \
      | sed -n 's/.*"expiresAt"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' \
      | head -1)"
    # No parseable expiry → treat as dead; a live entry always carries one.
    if [ -z "$exp" ] || [ "$exp" -le "$now_ms" ]; then
      security delete-generic-password -s "$svc" >/dev/null 2>&1 \
        && cleared=$((cleared + 1))
    fi
  done <<< "$entries"

  [ "$cleared" -gt 0 ] \
    && echo "Cleared $cleared stale per-config-dir Keychain entry(ies)"
  return 0
}

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

  # Refreshing the file is not enough on macOS — a stale per-config-dir entry
  # would still shadow it. Sweep here so "auth broke → run cc-credentials"
  # stays true on the host too, not just inside the sandbox.
  _cc_clear_stale_keychain_entries
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

# Add a global sbx network allow rule. As of sbx v0.33.0 global is the
# DEFAULT scope: pass RESOURCES bare for a global rule (`--sandbox <name>`
# scopes to one sandbox). The old `-g`/`--global` flag is deprecated — it
# still works but prints "Flag --global has been deprecated" to stderr,
# which pollutes the captured error output and will break outright once the
# flag is removed. (Earlier, v0.29.0–v0.31.x, scope was MANDATORY and the
# bare form exited non-zero; that requirement was reversed.)
# Failures are surfaced rather than swallowed: a discarded error here
# once let a breaking CLI change disable policy sync for every run with no
# signal (sync still printed "Synced N" while adding nothing).
# Idempotent: re-adding a covered rule is a no-op and exits 0.
_ola_policy_allow() {
  local resources="$1" out
  if ! out="$(sbx policy allow network "$resources" 2>&1)"; then
    echo "Error: 'sbx policy allow network $resources' failed: $out" >&2
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
    while IFS= read -r line || [ -n "$line" ]; do
      # Strip inline comments ('host  # note') then take the first
      # whitespace-delimited token as the host. Without this, comment words
      # leak into RESOURCES (comma/space-separated) and sbx parses them as
      # bogus domains — e.g. a 'ticket_classes' note triggered a duplicate-
      # rule error that aborted the whole sync. Blank and '#'-only lines
      # collapse to an empty host and are skipped.
      line="${line%%#*}"
      local host
      read -r host _ <<< "$line"
      [ -z "$host" ] && continue
      if _ola_policy_allow "$host,*.$host"; then
        count=$((count + 1))
      else
        failed=$((failed + 1))
      fi
    done < "$allowlist"
  fi

  # Auto-allow GitHub egress so the injected `gh`/GH_TOKEN auth works with no
  # allowlist.txt edit; the *. wildcard also covers api./codeload.github.com.
  if _ola_policy_allow "github.com,*.github.com"; then
    count=$((count + 1))
  else
    failed=$((failed + 1))
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

# Inject the host's GitHub CLI auth into a running sandbox so `gh` and plain
# git-over-HTTPS work there. Mirrors cc-credentials: reads the token fresh
# from the host (`gh auth token` resolves keyring/file/env the way gh itself
# would) on every create AND reconnect — gh tokens don't rotate-on-refresh
# the way the CC subscription token does, so this read IS the refresh.
# Non-fatal: a host with no `gh auth login` just gets a warning, never a
# reason to fail sandbox creation. Must run AFTER _ola_inject_sidecar, which
# overwrites (not appends) ~/.ola/agent.env — this appends GH_TOKEN to it;
# the login rc sources that file under `set -a`, so no `export` is needed.
_ola_inject_gh() {
  local name="$1"
  if ! command -v gh >/dev/null 2>&1; then
    echo "Warning: gh not found on host — run 'gh auth login' on the host first." >&2
    return 0
  fi
  local gh_token
  gh_token="$(gh auth token 2>/dev/null)"
  if [ -z "$gh_token" ]; then
    echo "Warning: gh auth token not found — run 'gh auth login' on the host first." >&2
    return 0
  fi
  local data
  data="$(printf 'GH_TOKEN=%s\n' "$gh_token" | base64)"
  sbx exec "$name" bash -c "echo '$data' | base64 -d >> \$HOME/.ola/agent.env" 2>/dev/null
  local tok_b64
  tok_b64="$(printf '%s' "$gh_token" | base64)"
  sbx exec "$name" bash -c "export GH_TOKEN=\$(echo '$tok_b64' | base64 -d); gh auth setup-git" 2>/dev/null
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

# Write ola's canonical Claude Code settings.json into a running sandbox.
# Deliberately minimal — and deliberately NOT a copy of the host settings.json.
# The docker sandbox is the isolation boundary, so Claude Code's *own* command
# sandbox is redundant inside it; worse, that sandbox confines writes to the
# worktree cwd, which silently blocks the ola-blocked marker (it lands in the
# agent folder, above the worktree) and any other cross-worktree write. Copying
# the host file would also drag in personal hooks/MCP. Keep it to exactly two
# keys: bypass permissions and skip the dangerous-mode prompt. No "sandbox".
_ola_inject_cc_settings() {
  local name="$1"
  local settings='{
  "permissions": {
    "defaultMode": "bypassPermissions"
  },
  "skipDangerousModePermissionPrompt": true
}'
  sbx exec "$name" bash -c 'mkdir -p "$HOME/.claude"' 2>/dev/null
  local data
  data="$(printf '%s' "$settings" | base64)"
  sbx exec "$name" bash -c "echo '$data' | base64 -d > \$HOME/.claude/settings.json" 2>/dev/null
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
  # ola owns the in-sandbox settings.json (minimal, no CC sandbox); never copy
  # the host's. See _ola_inject_cc_settings for why.
  _ola_inject_cc_settings "$name"

  # OpenHands: agent settings and CLI config. agent_settings.json is the
  # baseline copy; _ola_inject_oh_settings overwrites it with the resolved
  # endpoint (the host copy carries a stale, possibly-rotated base_url).
  local oh_dir="$HOME/.openhands"
  _ola_inject_file "$name" "$oh_dir/agent_settings.json" "\$HOME/.openhands/agent_settings.json" || true
  _ola_inject_file "$name" "$oh_dir/cli_config.json" "\$HOME/.openhands/cli_config.json" || true
}

# Patch the host OpenHands CLI settings with the resolved LLM endpoint and
# inject them into the sandbox. ola's `oh` backend drives the openhands CLI and
# writes its *own* per-task agent_settings.json under a per-task persistence
# dir (OPENHANDS_PERSISTENCE_DIR), so this host-level ~/.openhands copy is for
# *manual/interactive* `openhands` runs in the sandbox — whose host copy
# carries a stale base_url/key (the substrate IP rotates). Only the rotating
# identity fields are patched; the user's tools/condenser/prompt config in the
# template is preserved verbatim. Non-fatal: on any gap (no jq, no template,
# missing values) the verbatim host copy from _ola_inject_credentials plus the
# exported shell env remain as fallback.
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
# sees the same LLM_* values ola's backends load — they otherwise live only in
# ~/.ola/agent.env, which only ola's Python reads. Mirrors the backend's
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

# Target sandbox memory: 80% of the Docker VM, overriding sbx's 50% default so
# parallel agent runs get more headroom. 80% is computed off the same base sbx
# uses for its own "% of host" default — `docker info`'s MemTotal (the Docker VM
# size, not the Mac's physical RAM) — and capped at sbx's documented 32 GiB
# ceiling. The sandbox is a hard RAM wall with NO swap (overlay rootfs; see
# .claude/skills/sbx/SKILL.md), so leaving ~20% headroom matters: an overshoot
# is an instant OOM kill, not a slowdown. Echoes an sbx `-m` value (e.g.
# "12777m"), or nothing (non-zero) if the VM size can't be read, in which case
# the caller lets sbx apply its own default. Override with OLA_SBX_MEMORY (any
# sbx -m value, e.g. "24g"); the override bypasses the 32 GiB cap.
_ola_sbx_memory() {
  if [ -n "$OLA_SBX_MEMORY" ]; then
    printf '%s' "$OLA_SBX_MEMORY"
    return 0
  fi
  local total_bytes
  total_bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null)"
  case "$total_bytes" in
    ''|*[!0-9]*) return 1 ;;  # docker unavailable / unexpected format
  esac
  local mb=$(( total_bytes * 8 / 10 / 1048576 ))  # 80%, in MiB
  local cap=$(( 32 * 1024 ))                        # sbx's documented 32 GiB ceiling
  [ "$mb" -gt "$cap" ] && mb=$cap
  [ "$mb" -lt 1 ] && return 1
  printf '%dm' "$mb"
}

# Ensure a sandbox is ready to run ola in: authenticated sbx, agent/.env
# resolved + network policy synced, sandbox created (fresh) or reconnected,
# credentials/sidecar/oh-settings injected. Everything `ola-sandbox` does up
# to attaching, split out so `ola-monitor` can drive the same create-or-
# reconnect + inject path non-interactively (`sbx exec` instead of `sbx
# run`) without duplicating it. Callers attach (or exec into) "$name" on
# success.
_ola_sandbox_prepare() {
  local name="${1:?Usage: _ola_sandbox_prepare <sandbox_name>}"
  local code_dir="$(pwd)"
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
    _ola_inject_gh "$name"
    _ola_inject_oh_settings "$name" "$_env_blob"
    return 0
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

  # Size the sandbox at 80% of the Docker VM (vs sbx's 50% default); see
  # _ola_sbx_memory. Built as an array so an unreadable VM size cleanly omits
  # -m and falls back to the sbx default rather than passing a broken flag.
  local -a mem_arg
  local _mem
  if _mem="$(_ola_sbx_memory)"; then
    mem_arg=(-m "$_mem")
    echo "ola-sandbox: memory -m $_mem (80% of Docker VM; override with OLA_SBX_MEMORY)" >&2
  fi

  sbx create shell \
    --name "$name" \
    --template "$image" \
    "${mem_arg[@]}" \
    -q \
    "$project_dir" || {
    echo "Error: failed to create sandbox '$name'" >&2
    return 1
  }

  _ola_inject_credentials "$name"
  _ola_inject_sidecar "$name" "$_env_blob"
  _ola_inject_gh "$name"
  _ola_inject_oh_settings "$name" "$_env_blob"

  # Export the resolved env into the login shell and land in the project repo.
  _ola_setup_shell_rc "$name" "$code_dir"
  return 0
}

ola-sandbox() {
  local name="${1:?Usage: ola-sandbox <sandbox_name>}"
  _ola_sandbox_prepare "$name" || return 1

  # Attach to the sandbox (foreground, interactive). Re-attach by --name:
  # the positional `sbx run <name>` re-attach form was deprecated in sbx
  # v0.33.0 (the positional is now the agent), so identify the sandbox with
  # --name; the agent is read from the existing sandbox spec.
  sbx run --name "$name"
}

# ===== ola-monitor: host-side auth launcher-watcher =====
# NOT the old in-sandbox progress monitor — that concept was scrapped (see
# design-notes.md); deterministic progress is ola-top's job. ola-monitor's
# sole concern is auth recovery: only the host can run cc-credentials against
# the Keychain and re-inject into the sandbox, so the watcher runs here and
# launches ola *into* the sandbox, rather than living in-sandbox.

# Extract the -f/--agent-folder value from an `ola` argv, defaulting to
# ../agent (mirrors cli.py's own default) so the watcher polls the same
# marker path ola itself resolves and writes to.
_ola_monitor_agent_folder() {
  local val="../agent" prev="" a
  for a in "$@"; do
    case "$prev" in
      -f | --agent-folder) val="$a" ;;
    esac
    case "$a" in
      --agent-folder=*) val="${a#--agent-folder=}" ;;
    esac
    prev="$a"
  done
  printf '%s' "$val"
}

# Best-effort flat-JSON string field extraction (no jq dependency, mirrors
# _ola_blob_val's approach) — the marker shape is locked to a flat
# {"sandbox", "ts", "message"} object of strings, see
# scheduler._write_auth_escalation_marker.
_ola_monitor_marker_field() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 1
  grep -o "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$file" 2>/dev/null \
    | head -1 \
    | sed -E 's/.*:[[:space:]]*"(.*)"/\1/'
}

# Parse an ISO8601 UTC timestamp (as written by scheduler._utc_now_iso) into
# epoch seconds. Tries GNU date, then BSD/macOS date, for portability.
_ola_monitor_epoch() {
  local ts="$1" out
  out="$(date -u -d "$ts" +%s 2>/dev/null)" && { printf '%s' "$out"; return 0; }
  out="$(date -j -u -f "%Y-%m-%dT%H:%M:%S" "${ts%%.*}" +%s 2>/dev/null)" && {
    printf '%s' "$out"
    return 0
  }
  return 1
}

# Prune a list of prior heal epochs down to those still inside the thrash
# window ending at $now. Pure function so the thrash counter/window logic is
# testable without waiting real wall-clock time. Window is
# OLA_MONITOR_THRASH_WINDOW seconds (default 300 = 5 minutes), read at call
# time so tests can override it per-case.
_ola_monitor_prune_window() {
  local now="$1"
  shift
  local window="${OLA_MONITOR_THRASH_WINDOW:-300}" t
  for t in "$@"; do
    [ $((now - t)) -le "$window" ] && printf '%s\n' "$t"
  done
}

# Heal/relaunch decision: given the count of heals already inside the thrash
# window (NOT counting the one about to happen), decide whether to self-heal
# again or stop and flag thrash. Repeated re-heals in a short window are the
# signature of a concurrent rotator (another live `claude` session sharing
# the account) that a mechanical re-pull cannot win — see design-notes.md.
# Threshold is OLA_MONITOR_THRASH_MAX (default 3), read at call time.
_ola_monitor_decide() {
  local count="$1" max="${OLA_MONITOR_THRASH_MAX:-3}"
  if [ "$count" -ge "$max" ]; then
    echo "thrash"
  else
    echo "heal"
  fi
}

# Launch `ola <args>` into a sandbox and keep it authenticated unsupervised.
# Takes the SAME arguments as `ola` (see design-notes.md — "Decided —
# ola-monitor, reborn as the host-side auth launcher-watcher"):
#   (1) Launch  — ensure/create the sandbox + inject fresh credentials, then
#                 exec `ola <args>` inside it.
#   (2) Watch   — for the host-visible auth-escalation marker ola drops on a
#                 loud auth abort (<agent_folder>/monitor/auth-escalation.json).
#   (3) Heal    — cc-credentials + re-inject, delete the marker, relaunch.
#   (4) Thrash  — >= threshold re-heals within the window: stop, notify.
#   (5) Notify  — dead Keychain token (cc-credentials fails): stop, notify.
#   (6) Exit    — when ola completes cleanly (no marker), return its exit code.
# Scope is auth-only: no progress reporting of its own (that's ola-top's job)
# beyond the one-line launch ack and the heal/thrash/notify events themselves.
# The sandbox name isn't part of ola's own argv, so it's derived from the
# project checkout directory name (override with OLA_MONITOR_SANDBOX if the
# sandbox was created under a different name).
ola-monitor() {
  local code_dir="$(pwd)"
  local name="${OLA_MONITOR_SANDBOX:-$(basename "$code_dir")}"

  local agent_dir
  agent_dir="$(_ola_monitor_agent_folder "$@")"
  agent_dir="$(cd "$agent_dir" 2>/dev/null && pwd)" || {
    echo "Error: agent folder not found (resolved from -f/--agent-folder)." >&2
    return 1
  }
  local marker="$agent_dir/monitor/auth-escalation.json"

  echo "ola-monitor: supervising 'ola $*' in sandbox '$name'"

  _ola_sandbox_prepare "$name" || return 1

  local -a heal_epochs=()
  local rc
  while true; do
    sbx exec -w "$code_dir" "$name" env "OLA_SANDBOX_NAME=$name" ola "$@"
    rc=$?

    # No marker: ola exited on its own (clean completion or an unrelated
    # failure) — nothing for the watcher to do.
    [ -f "$marker" ] || return "$rc"

    local msg ts now
    msg="$(_ola_monitor_marker_field "$marker" message)"
    ts="$(_ola_monitor_marker_field "$marker" ts)"
    now="$(_ola_monitor_epoch "$ts")"
    [ -n "$now" ] || now="$(date -u +%s)"

    local -a kept=()
    local line
    while IFS= read -r line; do
      [ -n "$line" ] && kept+=("$line")
    done < <(_ola_monitor_prune_window "$now" "${heal_epochs[@]}")
    heal_epochs=("${kept[@]}")

    if [ "$(_ola_monitor_decide "${#heal_epochs[@]}")" = "thrash" ]; then
      echo "ola-monitor: auth broke ${#heal_epochs[@]} times in the last" \
        "${OLA_MONITOR_THRASH_WINDOW:-300}s — something else is using this" \
        "account (a concurrent claude session?). Run ola alone. Stopping." >&2
      return 1
    fi

    echo "ola-monitor: auth escalation (${msg:-no message}) — re-healing" \
      "credentials for '$name'." >&2

    if ! cc-credentials; then
      echo "ola-monitor: no valid Keychain token found — log in (run" \
        "'claude' on the host) then re-run ola-monitor to resume." >&2
      return 1
    fi

    _ola_inject_credentials "$name"
    heal_epochs+=("$now")
    rm -f "$marker"
  done
}
