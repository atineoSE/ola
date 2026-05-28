#!/usr/bin/env bats
# Smoke test: git worktree works correctly inside the ola-sandbox container.
#
# The parallel scheduler creates one git worktree per task (see
# src/ola/worktree.py). Worktrees have one notable quirk inside any
# container/bind-mount setup: the secondary worktree's ``.git`` is a FILE
# containing ``gitdir: <main-repo>/.git/worktrees/<name>``, not a directory.
# If that gitdir reference resolves through a path that is not visible
# inside the sandbox, every git command in the worktree fails. This test
# exercises the full lifecycle (create → commit → cherry-pick back →
# remove) inside the sandbox to catch any such regression.
#
# Run:   bats tests/test_sandbox_worktree.bats
# Env:   OLA_SBX_IMAGE — override template image
#        SBX_TEST_TIMEOUT — seconds to wait for sandbox creation (default: 120)

TIMEOUT="${SBX_TEST_TIMEOUT:-120}"
IMAGE="${OLA_SBX_IMAGE:-ghcr.io/atineose/ola:latest}"

setup_file() {
  if ! command -v sbx &>/dev/null; then
    skip "sbx CLI not found"
  fi

  export SBX_NAME="ola-worktree-smoke"
  export TIMEOUT IMAGE
  export TMPDIR_TEST="$(mktemp -d)"
  export REPO_DIR="$TMPDIR_TEST/repo"
  mkdir -p "$REPO_DIR"

  local template_flag=()
  if [ -n "$IMAGE" ]; then
    template_flag=(--template "$IMAGE")
  fi

  local create_err
  create_err="$(sbx create shell \
    --name "$SBX_NAME" \
    "${template_flag[@]}" \
    -m 4g \
    -q \
    "$TMPDIR_TEST" 2>&1)" || {
    rm -rf "$TMPDIR_TEST"
    echo "$create_err" >&2
    return 1
  }
}

teardown_file() {
  if command -v sbx &>/dev/null; then
    timeout "$TIMEOUT" sbx stop "$SBX_NAME" 2>/dev/null || true
    # --force: bats runs without a TTY, so 'sbx rm' would otherwise prompt
    # for confirmation, fail, and leak the sandbox into the next run.
    timeout "$TIMEOUT" sbx rm --force "$SBX_NAME" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR_TEST"
}

setup() {
  command -v sbx &>/dev/null || skip "sbx CLI not found"
}

@test "git worktree lifecycle inside ola-sandbox" {
  # One self-contained script: init repo, add a worktree, commit inside it,
  # cherry-pick back onto main, and remove the worktree. Any environmental
  # quirk (missing .git/worktrees/<name> resolution, read-only mount, etc.)
  # surfaces here as a non-zero exit.
  run sbx exec "$SBX_NAME" bash -c "
    set -euo pipefail
    cd $REPO_DIR

    git init -b main >/dev/null
    git config user.email test@example.com
    git config user.name Test
    git config commit.gpgsign false
    echo hello > greet.txt
    git add -A
    git commit -m initial >/dev/null

    git worktree add -b feature/smoke $REPO_DIR/wt HEAD
    test -d $REPO_DIR/wt
    # The secondary worktree's .git is a FILE pointing back at the main
    # repo's .git/worktrees/<name>; if this resolution path is broken by
    # the sandbox mount, every subsequent git command in the worktree fails.
    test -f $REPO_DIR/wt/.git
    grep -q '^gitdir:' $REPO_DIR/wt/.git

    cd $REPO_DIR/wt
    git rev-parse --is-inside-work-tree | grep -q true
    echo world > new.txt
    git add -A
    git commit -m 'smoke: add new file' >/dev/null
    sha=\$(git rev-parse HEAD)

    cd $REPO_DIR
    git cherry-pick \$sha
    test -f $REPO_DIR/new.txt
    grep -q world $REPO_DIR/new.txt

    git worktree remove --force $REPO_DIR/wt
    test ! -e $REPO_DIR/wt
    ! git worktree list | grep -q $REPO_DIR/wt
  "
  [ "$status" -eq 0 ] || { echo "$output" >&2; false; }
}
