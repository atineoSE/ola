#!/usr/bin/env bash
#
# Symlink the repo-owned `ola-plan` skill into the global skill directories of
# the agent harnesses, so "create the ola plan for this" works from any planning
# session. The skill is owned here; the symlinks just point back at this repo,
# so editing the skill in the repo updates every harness at once.
#
# Targets:
#   ~/.claude/skills/ola-plan     → Claude Code
#   ~/.openhands/skills/ola-plan  → OpenHands
#
# The source is the repo's MAIN working tree (resolved via git), so running this
# from a linked worktree still points the symlinks at the canonical checkout.

set -euo pipefail

# Canonical repo root = parent of the shared .git dir (works from any worktree).
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
git_common_dir="$(git -C "$script_dir" rev-parse --path-format=absolute --git-common-dir)"
repo_root="$(dirname "$git_common_dir")"
src="$repo_root/.claude/skills/ola-plan"

if [ ! -e "$src" ]; then
  echo "note: skill source $src does not exist yet (not on the checked-out" \
       "branch). The symlinks will be created and resolve once it lands on the" \
       "main checkout." >&2
fi

link_into() {
  local skills_dir="$1" label="$2"
  if [ ! -d "$skills_dir" ]; then
    echo "skip: $label skills dir $skills_dir not found" >&2
    return 0
  fi
  local target="$skills_dir/ola-plan"
  ln -snf "$src" "$target"
  echo "linked: $target -> $src ($label)"
}

link_into "$HOME/.claude/skills" "Claude Code"
link_into "$HOME/.openhands/skills" "OpenHands"
