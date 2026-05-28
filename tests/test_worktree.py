"""Tests for ola.worktree — create / commit / merge_back / cleanup helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ola import worktree
from ola.plan import enumerate_tasks, set_task_checked, task_is_checked


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def _init_repo(repo: Path) -> None:
    """Set up a minimal git repo with an initial commit on the main branch."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text("")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")


def _setup_folder(repo: Path, name: str, plan: str) -> Path:
    folder = repo / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "PLAN.md").write_text(plan)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"add {name}")
    return folder


def _log_oneline(repo: Path, ref: str = "main") -> list[str]:
    out = subprocess.run(
        ["git", "log", "--oneline", ref],
        cwd=str(repo),
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [ln for ln in out.splitlines() if ln.strip()]


class TestCreate:
    def test_creates_worktree_dir_and_branch(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        folder = _setup_folder(repo, "agent-folder", "- [ ] One task\n")

        task = enumerate_tasks(folder)[0]
        wt = worktree.create(folder, task.task_id)

        assert wt == folder / ".ola" / "worktrees" / task.task_id
        assert wt.is_dir()
        # The worktree mirrors the repo's layout — PLAN.md lives under <wt>/agent-folder/
        assert (wt / folder.name / "PLAN.md").read_text().startswith("- [ ] One task")
        # The branch should exist
        branches = subprocess.run(
            ["git", "branch", "--list"], cwd=str(repo), capture_output=True, check=True
        ).stdout.decode()
        assert f"ola/{folder.name}/{task.task_id}" in branches


class TestCommit:
    def test_stages_and_commits_dirty_changes(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        folder = _setup_folder(repo, "agent-folder", "- [ ] Solo\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(folder, task.task_id)

        (wt / "new_file.txt").write_text("hi")
        sha = worktree.commit(wt, "agent: did the thing")

        # SHA points to a commit on the worktree's branch with the new file.
        head = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(wt),
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        assert sha == head
        log = _log_oneline(wt, "HEAD")
        assert any("agent: did the thing" in line for line in log)

    def test_returns_head_when_nothing_to_commit(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        folder = _setup_folder(repo, "agent-folder", "- [ ] Solo\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(folder, task.task_id)

        before = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(wt),
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        sha = worktree.commit(wt, "agent: no-op")
        assert sha == before  # No new commit was created


class TestMergeBack:
    def test_two_parallel_tasks_with_plan_ticks_via_set_task_checked(self, tmp_path):
        """End-to-end: two disjoint-file tasks both tick their checkbox.

        Verifies the contract that PLAN.md ticks on the main branch arrive via
        ``set_task_checked`` rather than via the cherry-pick itself.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        folder = _setup_folder(repo, "agent-folder", "- [ ] Task A\n- [ ] Task B\n")
        task_a, task_b = enumerate_tasks(folder)

        wt_a = worktree.create(folder, task_a.task_id)
        wt_b = worktree.create(folder, task_b.task_id)

        # Each "worker" writes a unique file AND ticks its own checkbox in
        # its worktree's PLAN.md (the agent's completion signal).
        (wt_a / "file_a.txt").write_text("hello A")
        set_task_checked(wt_a / folder.name, task_a.task_id, True)
        sha_a = worktree.commit(wt_a, "agent: task A done")

        (wt_b / "file_b.txt").write_text("hello B")
        set_task_checked(wt_b / folder.name, task_b.task_id, True)
        sha_b = worktree.commit(wt_b, "agent: task B done")

        plan_rel = f"{folder.name}/PLAN.md"

        # Scheduler-style propagation for task A.
        returned_a = worktree.merge_back(wt_a, repo, exclude_paths=[plan_rel])
        assert returned_a == sha_a
        # PLAN.md ticks were dropped by the exclude — main still has neither tick.
        assert task_is_checked(folder, task_a.task_id) is False
        assert task_is_checked(folder, task_b.task_id) is False
        # But file_a is staged
        assert (repo / "file_a.txt").read_text() == "hello A"
        # Apply the tick separately and commit
        set_task_checked(folder, task_a.task_id, True)
        _git(repo, "add", plan_rel)
        _git(repo, "commit", "-C", sha_a)

        # Propagation for task B.
        returned_b = worktree.merge_back(wt_b, repo, exclude_paths=[plan_rel])
        assert returned_b == sha_b
        # B's tick is also dropped — A's tick persists from the previous commit,
        # B's tick is not yet present.
        assert task_is_checked(folder, task_a.task_id) is True
        assert task_is_checked(folder, task_b.task_id) is False
        assert (repo / "file_b.txt").read_text() == "hello B"
        set_task_checked(folder, task_b.task_id, True)
        _git(repo, "add", plan_rel)
        _git(repo, "commit", "-C", sha_b)

        # Final state: both files landed, both checkboxes ticked.
        assert task_is_checked(folder, task_a.task_id) is True
        assert task_is_checked(folder, task_b.task_id) is True

        # main has initial + folder-add + task A + task B = 4 commits.
        log = _log_oneline(repo, "main")
        assert len(log) == 4
        assert any("agent: task A done" in line for line in log)
        assert any("agent: task B done" in line for line in log)

        # Cleanup removes worktrees.
        worktree.cleanup(wt_a, keep_on_failure=False)
        worktree.cleanup(wt_b, keep_on_failure=False)
        assert not wt_a.exists()
        assert not wt_b.exists()

    def test_returns_sha_for_commit_minus_C(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        folder = _setup_folder(repo, "agent-folder", "- [ ] Solo\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(folder, task.task_id)
        (wt / "added.txt").write_text("x")
        sha = worktree.commit(wt, "agent: solo")
        returned = worktree.merge_back(wt, repo, exclude_paths=[])
        assert returned == sha
        # Finish the commit on main
        _git(repo, "commit", "-C", sha)
        log = _log_oneline(repo, "main")
        assert any("agent: solo" in line for line in log)

    def test_conflict_outside_exclude_paths_raises(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        folder = _setup_folder(repo, "agent-folder", "- [ ] Task A\n- [ ] Task B\n")
        # Add a shared file both workers will fight over.
        shared = repo / "shared.txt"
        shared.write_text("base line\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "add shared")

        task_a, task_b = enumerate_tasks(folder)
        wt_a = worktree.create(folder, task_a.task_id)
        wt_b = worktree.create(folder, task_b.task_id)

        # Both modify the same line of shared.txt — guaranteed conflict.
        (wt_a / "shared.txt").write_text("A's line\n")
        sha_a = worktree.commit(wt_a, "agent: A clobbers shared")
        (wt_b / "shared.txt").write_text("B's line\n")
        sha_b = worktree.commit(wt_b, "agent: B clobbers shared")

        plan_rel = f"{folder.name}/PLAN.md"

        # A goes first cleanly.
        worktree.merge_back(wt_a, repo, exclude_paths=[plan_rel])
        _git(repo, "commit", "-C", sha_a)

        # B conflicts on shared.txt (not in exclude_paths).
        with pytest.raises(worktree.MergeBackConflict) as exc:
            worktree.merge_back(wt_b, repo, exclude_paths=[plan_rel])
        assert any("shared.txt" in p for p in exc.value.conflicted_paths)
        assert exc.value.sha == sha_b

        # Working tree was rolled back — no tracked changes staged or
        # unstaged. (`-uno` skips the untracked .ola/ worktrees directory.)
        status = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert status.strip() == ""


class TestCleanup:
    def test_keep_on_failure_preserves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        folder = _setup_folder(repo, "agent-folder", "- [ ] One\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(folder, task.task_id)

        worktree.cleanup(wt, keep_on_failure=True)
        assert wt.exists()

    def test_removes_when_keep_false(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        folder = _setup_folder(repo, "agent-folder", "- [ ] One\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(folder, task.task_id)

        worktree.cleanup(wt, keep_on_failure=False)
        assert not wt.exists()
        # And `git worktree list` no longer shows it.
        listing = subprocess.run(
            ["git", "worktree", "list"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert str(wt) not in listing

    def test_no_op_when_missing(self, tmp_path):
        worktree.cleanup(tmp_path / "does-not-exist", keep_on_failure=False)
