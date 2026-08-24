#!/usr/bin/env bash
#
# EXAMPLE agent-folder file: <agent-folder>/run-init.sh
#
# Preconditions that must hold before any task is dispatched. `ola` runs it
# itself at startup — once per run, from the project repo, inside the sandbox
# only. A non-zero exit aborts the run before the first task.
#
# The case it exists for: anything a task starts OUTLIVES the task. A
# daemonized process reparents to PID 1, so nothing that kills the task agent,
# ola, or the `sbx exec` ever stops it — and removing the worktree does not
# either, since it keeps running on unlinked inodes. Every crashed, killed or
# interrupted run can therefore leak a live server. This sweeps them up at the
# next run's boundary.
#
# This example reaps PostgreSQL clusters left in per-task worktrees. Copy the
# shape, not the package: identify the *supervisor* process (not its children),
# match on state your worktrees own, and kill only that.

set -euo pipefail

reaped=0
for pid in $(pgrep -x postgres 2>/dev/null || true); do
  # ppid==1 → the postmaster, not one of its background workers.
  ppid="$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null || echo)"
  [ "$ppid" = "1" ] || continue
  cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || echo)"
  case "$cwd" in
    */.ola/worktrees/*|*"(deleted)") ;;   # ours, or its dir is already gone
    *) continue ;;
  esac
  # SIGQUIT = immediate shutdown, no checkpoint: the data dir is throwaway and
  # may already be unlinked, so a clean checkpoint has nothing to write to.
  kill -QUIT "$pid" 2>/dev/null || true
  reaped=$((reaped + 1))
  echo "run-init: reaped stale postgres $pid ($cwd)"
done

if [ "$reaped" -gt 0 ]; then
  sleep 2
  for pid in $(pgrep -x postgres 2>/dev/null || true); do
    ppid="$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null || echo)"
    [ "$ppid" = "1" ] && kill -KILL "$pid" 2>/dev/null || true
  done
fi
echo "run-init: ok ($reaped reaped)"
