#!/usr/bin/env bats
# Smoke test: the two-repo per-task worktree flow works inside the ola-sandbox.
#
# The parallel scheduler creates one git worktree per task (see
# src/ola/worktree.py), but the two repos it spans differ in role:
#   * the PROJECT repo (process cwd) — worktrees branch from its HEAD and the
#     agent's code is cherry-picked back onto it;
#   * the AGENT folder — a sibling repo holding the numbered plan folder and its
#     PLAN.md, whose checkbox tick is committed there separately.
# The live PLAN.md is copied into <worktree>/.ola/PLAN.md for the agent to tick,
# and .ola/ is git-excluded so that copy never rides the cherry-pick.
#
# Worktrees have one notable quirk inside any container/bind-mount setup: the
# secondary worktree's ``.git`` is a FILE containing
# ``gitdir: <main-repo>/.git/worktrees/<name>``, not a directory. If that gitdir
# reference resolves through a path not visible inside the sandbox, every git
# command in the worktree fails. This test exercises the full lifecycle
# (create → stage plan copy → commit code → cherry-pick back → tick agent
# folder → remove) across both repos inside the sandbox to catch any such
# regression.
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
  # Two sibling repos under the bind-mounted tree: the project repo (worktree
  # source) and the agent folder (plan + ticks).
  export PROJECT_DIR="$TMPDIR_TEST/project"
  export AGENT_DIR="$TMPDIR_TEST/agent"
  mkdir -p "$PROJECT_DIR" "$AGENT_DIR"

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

@test "two-repo worktree lifecycle inside ola-sandbox" {
  # One self-contained script that mirrors the scheduler's propagation:
  #   1. init the project repo and the sibling agent folder repo;
  #   2. add a worktree of the PROJECT repo (branch named after the stage);
  #   3. stage the agent folder's PLAN.md into <worktree>/.ola/PLAN.md and tick
  #      it there — .ola/ is git-excluded so the tick stays out of the commit;
  #   4. make a code change in the worktree and commit it;
  #   5. cherry-pick the code back onto the project repo's main;
  #   6. tick + commit the agent folder's PLAN.md;
  #   7. remove the worktree.
  # Any environmental quirk (missing .git/worktrees/<name> resolution, read-only
  # mount, etc.) surfaces here as a non-zero exit.
  run sbx exec "$SBX_NAME" bash -c "
    set -euo pipefail

    # --- project repo ---
    cd $PROJECT_DIR
    git init -b main >/dev/null
    git config user.email test@example.com
    git config user.name Test
    git config commit.gpgsign false
    echo base > base.txt
    git add -A
    git commit -m initial >/dev/null
    # .ola/ holds runtime artifacts (worktrees, the staged plan copy); keep it
    # out of git, shared across worktrees via .git/info/exclude.
    printf '.ola/\n' >> .git/info/exclude

    # --- agent folder (sibling repo, holds the plan and its ticks) ---
    cd $AGENT_DIR
    git init -b main >/dev/null
    git config user.email test@example.com
    git config user.name Test
    git config commit.gpgsign false
    mkdir -p 01-stage
    printf -- '- [ ] Do the thing\n' > 01-stage/PLAN.md
    git add -A
    git commit -m 'add 01-stage' >/dev/null

    # --- create the project worktree for the task ---
    cd $PROJECT_DIR
    wt=$PROJECT_DIR/.ola/worktrees/t-smoke
    git worktree add -b ola/01-stage/t-smoke \$wt HEAD
    test -d \$wt
    # The secondary worktree's .git is a FILE pointing back at the main repo's
    # .git/worktrees/<name>; if this resolution path is broken by the sandbox
    # mount, every subsequent git command in the worktree fails.
    test -f \$wt/.git
    grep -q '^gitdir:' \$wt/.git

    # --- stage the live PLAN.md into the worktree's .ola and tick it ---
    mkdir -p \$wt/.ola
    cp $AGENT_DIR/01-stage/PLAN.md \$wt/.ola/PLAN.md
    sed -i 's/- \[ \]/- [x]/' \$wt/.ola/PLAN.md
    grep -q '\- \[x\] Do the thing' \$wt/.ola/PLAN.md

    # --- code change + commit inside the worktree ---
    cd \$wt
    git rev-parse --is-inside-work-tree | grep -q true
    echo world > new.txt
    git add -A
    # The excluded .ola/ copy must NOT have been staged.
    ! git diff --cached --name-only | grep -q '\.ola/'
    git commit -m 'feat: do the thing' >/dev/null
    sha=\$(git rev-parse HEAD)

    # --- cherry-pick the code back onto the project repo ---
    cd $PROJECT_DIR
    git cherry-pick -n \$sha
    git commit -C \$sha >/dev/null
    test -f $PROJECT_DIR/new.txt
    grep -q world $PROJECT_DIR/new.txt

    # --- tick + commit the agent folder's PLAN.md (separate repo) ---
    cd $AGENT_DIR
    sed -i 's/- \[ \]/- [x]/' 01-stage/PLAN.md
    git add 01-stage/PLAN.md
    git commit -m 'ola: 01-stage t-smoke' >/dev/null
    grep -q '\- \[x\] Do the thing' 01-stage/PLAN.md

    # --- remove the worktree ---
    cd $PROJECT_DIR
    git worktree remove --force \$wt
    test ! -e \$wt
    ! git worktree list | grep -q \$wt
  "
  [ "$status" -eq 0 ] || { echo "$output" >&2; false; }
}
