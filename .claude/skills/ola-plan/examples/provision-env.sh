#!/usr/bin/env bash
#
# EXAMPLE agent-folder file: <agent-folder>/provision.sh
#
# Companion to examples/provision.sh. That one *installs* something the image
# does not ship; this one *settles the environment* the tasks will run in, so
# that no task agent has to work it out. The distinction matters because a
# wrong environment does not fail once — every fresh-context task rediscovers
# it, invents its own workaround, and the workarounds conflict. That is the
# thrash this file exists to prevent.
#
# The case here is Python venvs on a host-shared mount, but the shape
# generalises to anything whose default location or default toolchain is wrong
# inside the sandbox: build caches, node_modules, a compiler's temp dir.
#
#   1. The project directory is bind-mounted from the host, so it is NOT a
#      native Linux filesystem. Hardlinks across it fail ("Invalid cross-device
#      link"), so every `uv sync` silently degrades to a full copy, and an
#      editable install of the project can fail outright. A venv built on the
#      macOS host is worse still: `.venv` then carries a macOS CPython this
#      Linux container cannot execute, and the failure reads as a broken
#      project rather than as a wrong interpreter.
#   2. /home/agent is native overlayfs and shares a filesystem with uv's cache,
#      so hardlinks work there and a per-worktree venv costs ~0.15s once the
#      cache is warm.
#   3. The fix is published through /etc/sandbox-persistent.sh. The sandbox
#      image sets BASH_ENV to that path, so EVERY non-interactive bash sources
#      it — the agent's own shell-outs included — with no login shell and no
#      per-task setup. It is the one place an agent-folder script can change
#      the environment that every later task sees.
#   4. Warm the shared cache here, so the first task does not pay for it and
#      then hit its timeout before it can tick a checkbox.
#
# Caveats worth knowing before copying this:
#   - BASH_ENV is bash-only. A shell *function* defined here is invisible to
#     `sh` and to anything that exec's the binary directly (a Python subprocess
#     call, a Node spawn). Prefer exporting a plain variable when one exists;
#     use a function only when the value must be computed per directory, as
#     below.
#   - Write the whole file (`tee`, not `>>`). This runs on every create AND
#     reconnect, so appending would grow the file without bound.
#
# The ola image already ships uv, so there is nothing to install here. See
# examples/provision.sh for the guarded-install shape when there is.

set -euo pipefail

# --- 1. prove the problem rather than assuming it ----------------------------
# If a future image mounts the project natively, this whole file becomes a
# no-op worth deleting — so state the fact instead of trusting the comment.
probe=".ola-linkprobe.$$"
: > "$probe"
if ln "$probe" "$probe.hard" 2>/dev/null; then
  echo "provision.sh: project mount supports hardlinks — the venv redirect may no longer be needed"
  rm -f "$probe.hard"
fi
rm -f "$probe"

# --- 2. keep venvs off the shared mount, for every later shell ---------------
# uv is wrapped rather than UV_PROJECT_ENVIRONMENT exported once, so the venv
# follows the directory a command actually runs in (each task has its own
# worktree) rather than the one its shell happened to start in. Set
# UV_PROJECT_ENVIRONMENT_OVERRIDE to opt out for a single command.
sudo tee /etc/sandbox-persistent.sh >/dev/null <<'HOOK'
ola_uv_env() {
  local root
  root="$(git rev-parse --show-toplevel 2>/dev/null)" || root="$PWD"
  printf %s "/home/agent/venvs/$(basename "$root")"
}

uv() {
  UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT_OVERRIDE:-$(ola_uv_env)}" command uv "$@"
}
export -f ola_uv_env uv
HOOK
sudo chmod 0644 /etc/sandbox-persistent.sh

# --- 3. warm the cache so the first task does not pay for it -----------------
# Each worktree still builds its own venv, but from a populated cache on the
# same filesystem, which is what makes that build near-instant.
# shellcheck source=/dev/null
. /etc/sandbox-persistent.sh
if [ -d backend ]; then
  ( cd backend && uv sync --project . >/dev/null )
fi

echo "provision.sh: ok (uv $(command uv --version | awk '{print $2}'), venvs under /home/agent/venvs)"
