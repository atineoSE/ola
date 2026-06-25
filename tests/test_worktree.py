"""Tests for ola.worktree — create / commit / merge_back / cleanup helpers.

The worktree primitive now spans two repos: per-task worktrees branch from the
*project* repo (where the agent's code lands), while the PLAN.md checkbox lives
in a separate *agent folder* repo and is ticked there by the scheduler. These
tests model that split: code rides ``merge_back`` onto the project repo, ticks
are applied with ``set_task_checked`` directly on the agent folder.
"""

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


def _two_repos(tmp_path: Path, plan: str) -> tuple[Path, Path, Path]:
    """Return (project_repo, agent_root, folder) for a two-repo setup."""
    project = tmp_path / "project"
    _init_repo(project)
    agent_root = tmp_path / "agent"
    _init_repo(agent_root)
    folder = _setup_folder(agent_root, "agent-folder", plan)
    return project, agent_root, folder


def _log_oneline(repo: Path, ref: str = "main") -> list[str]:
    out = subprocess.run(
        ["git", "log", "--oneline", ref],
        cwd=str(repo),
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [ln for ln in out.splitlines() if ln.strip()]


def _staged(repo: Path) -> list[str]:
    """Paths staged in *repo*'s index relative to HEAD (sorted)."""
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    ).stdout.decode()
    return sorted(p for p in out.splitlines() if p.strip())


class TestCreate:
    def test_creates_worktree_dir_and_branch(self, tmp_path):
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] One task\n")

        task = enumerate_tasks(folder)[0]
        wt = worktree.create(project, folder, task.task_id)

        # The worktree lives under the project repo's .ola/, not the folder's.
        assert wt == project / ".ola" / "worktrees" / task.task_id
        assert wt.is_dir()
        # The project repo has no numbered plan folder of its own.
        assert not (wt / folder.name).exists()
        # The branch is created in the project repo, named after the stage.
        branches = subprocess.run(
            ["git", "branch", "--list"],
            cwd=str(project),
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert f"ola/{folder.name}/{task.task_id}" in branches


class TestCommit:
    def test_stages_and_commits_dirty_changes(self, tmp_path):
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] Solo\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(project, folder, task.task_id)

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
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] Solo\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(project, folder, task.task_id)

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
    def test_two_parallel_tasks_code_lands_and_ticks_apply_separately(self, tmp_path):
        """End-to-end: two disjoint-file tasks land code on the project repo
        while their checkbox ticks are applied to the agent folder.

        Verifies the contract that project code arrives via ``merge_back`` while
        PLAN.md ticks arrive via ``set_task_checked`` on the *agent folder* — the
        two never travel together.
        """
        project, agent_root, folder = _two_repos(
            tmp_path, "- [ ] Task A\n- [ ] Task B\n"
        )
        task_a, task_b = enumerate_tasks(folder)

        wt_a = worktree.create(project, folder, task_a.task_id)
        wt_b = worktree.create(project, folder, task_b.task_id)

        # Each "worker" writes a unique file in its project worktree.
        (wt_a / "file_a.txt").write_text("hello A")
        sha_a = worktree.commit(wt_a, "agent: task A done")

        (wt_b / "file_b.txt").write_text("hello B")
        sha_b = worktree.commit(wt_b, "agent: task B done")

        plan_rel = f"{folder.name}/PLAN.md"

        # Scheduler-style propagation for task A: code onto project, tick onto
        # the agent folder.
        returned_a = worktree.merge_back(wt_a, project)
        assert returned_a == sha_a
        # Neither tick is present yet — merge_back only moved code.
        assert task_is_checked(folder, task_a.task_id) is False
        assert task_is_checked(folder, task_b.task_id) is False
        # file_a is staged on the project repo.
        assert (project / "file_a.txt").read_text() == "hello A"
        _git(project, "commit", "-C", sha_a)
        # Apply the tick on the agent folder and commit it there.
        set_task_checked(folder, task_a.task_id, True)
        _git(agent_root, "add", plan_rel)
        _git(agent_root, "commit", "-m", f"ola: {folder.name} {task_a.task_id}")

        # Propagation for task B.
        returned_b = worktree.merge_back(wt_b, project)
        assert returned_b == sha_b
        assert (project / "file_b.txt").read_text() == "hello B"
        _git(project, "commit", "-C", sha_b)
        set_task_checked(folder, task_b.task_id, True)
        _git(agent_root, "add", plan_rel)
        _git(agent_root, "commit", "-m", f"ola: {folder.name} {task_b.task_id}")

        # Final state: both files on the project repo, both checkboxes ticked
        # in the agent folder.
        assert task_is_checked(folder, task_a.task_id) is True
        assert task_is_checked(folder, task_b.task_id) is True

        # The project repo has initial + task A + task B = 3 commits (no
        # folder-add — the project repo carries no plan folder).
        plog = _log_oneline(project, "main")
        assert len(plog) == 3
        assert any("agent: task A done" in line for line in plog)
        assert any("agent: task B done" in line for line in plog)

        # The agent folder has initial + folder-add + two tick commits.
        alog = _log_oneline(agent_root, "main")
        assert len(alog) == 4

        # Cleanup removes worktrees.
        worktree.cleanup(wt_a, keep_on_failure=False)
        worktree.cleanup(wt_b, keep_on_failure=False)
        assert not wt_a.exists()
        assert not wt_b.exists()

    def test_returns_sha_for_commit_minus_C(self, tmp_path):
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] Solo\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(project, folder, task.task_id)
        (wt / "added.txt").write_text("x")
        sha = worktree.commit(wt, "agent: solo")
        returned = worktree.merge_back(wt, project)
        assert returned == sha
        # Finish the commit on the project repo.
        _git(project, "commit", "-C", sha)
        log = _log_oneline(project, "main")
        assert any("agent: solo" in line for line in log)

    def test_identical_add_is_noop(self, tmp_path):
        """An incoming add of a path another task already landed *identically*
        is no diff against HEAD, so it drops out of the merge — only the task's
        own new file is staged. This is the shared empty ``__init__.py`` case."""
        project, _agent_root, folder = _two_repos(
            tmp_path, "- [ ] Task A\n- [ ] Task B\n"
        )
        task_a, task_b = enumerate_tasks(folder)

        # Task B branches off the original HEAD and adds an empty package
        # marker plus its own module.
        wt_b = worktree.create(project, folder, task_b.task_id)
        (wt_b / "pkg").mkdir()
        (wt_b / "pkg" / "__init__.py").write_text("")
        (wt_b / "pkg" / "b.py").write_text("b\n")
        sha_b = worktree.commit(wt_b, "agent: task B")

        # Task A has already landed an identical empty __init__.py (tracked) on
        # the project repo.
        (project / "pkg").mkdir()
        (project / "pkg" / "__init__.py").write_text("")
        (project / "pkg" / "a.py").write_text("a\n")
        _git(project, "add", "-A")
        _git(project, "commit", "-m", "task A landed pkg")

        # Merging B back: the identical __init__.py add is a no-op; only b.py
        # is staged. The old cherry-pick apply would have had nothing to add for
        # __init__.py either, but a 3-way merge makes that explicit and robust.
        assert worktree.merge_back(wt_b, project) == sha_b
        assert _staged(project) == ["pkg/b.py"]
        _git(project, "commit", "-C", sha_b)
        assert (project / "pkg" / "b.py").read_text() == "b\n"
        assert (project / "pkg" / "a.py").read_text() == "a\n"

    def test_identical_add_untracked_collision_does_not_abort(self, tmp_path):
        """An incoming add whose path already exists *untracked* in the project
        working tree reconciles instead of aborting — the exit-128 case the old
        ``cherry-pick -n`` apply died on."""
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] Task B\n")
        task_b = enumerate_tasks(folder)[0]

        wt_b = worktree.create(project, folder, task_b.task_id)
        (wt_b / "pkg").mkdir()
        (wt_b / "pkg" / "__init__.py").write_text("")
        (wt_b / "pkg" / "b.py").write_text("b\n")
        sha_b = worktree.commit(wt_b, "agent: task B")

        # A byte-identical __init__.py sits *untracked* in the project working
        # tree (e.g. left by a sibling task's rolled-back attempt). cherry-pick
        # would refuse with "untracked working tree files would be overwritten".
        (project / "pkg").mkdir()
        (project / "pkg" / "__init__.py").write_text("")

        # The 3-way merge ignores the working tree and reconciles cleanly.
        assert worktree.merge_back(wt_b, project) == sha_b
        _git(project, "commit", "-C", sha_b)
        assert (project / "pkg" / "b.py").read_text() == "b\n"
        assert (project / "pkg" / "__init__.py").read_text() == ""

    def test_auto_resolves_disjoint_edits_to_shared_file(self, tmp_path):
        """Non-overlapping edits to the *same* file are 3-way auto-merged rather
        than treated as a conflict — git already knows how to reconcile them."""
        project, _agent_root, folder = _two_repos(
            tmp_path, "- [ ] Task A\n- [ ] Task B\n"
        )
        (project / "shared.txt").write_text("l1\nl2\nl3\nl4\nl5\n")
        _git(project, "add", "-A")
        _git(project, "commit", "-m", "add shared")

        task_a, task_b = enumerate_tasks(folder)
        wt_a = worktree.create(project, folder, task_a.task_id)
        wt_b = worktree.create(project, folder, task_b.task_id)

        # A edits the top line, B edits the bottom line — disjoint hunks.
        (wt_a / "shared.txt").write_text("TOP\nl2\nl3\nl4\nl5\n")
        sha_a = worktree.commit(wt_a, "agent: A edits top")
        (wt_b / "shared.txt").write_text("l1\nl2\nl3\nl4\nBOTTOM\n")
        sha_b = worktree.commit(wt_b, "agent: B edits bottom")

        worktree.merge_back(wt_a, project)
        _git(project, "commit", "-C", sha_a)
        # B's disjoint edit auto-merges on top of A's already-landed change.
        worktree.merge_back(wt_b, project)
        _git(project, "commit", "-C", sha_b)

        assert (project / "shared.txt").read_text() == "TOP\nl2\nl3\nl4\nBOTTOM\n"

    def test_conflict_raises(self, tmp_path):
        project, _agent_root, folder = _two_repos(
            tmp_path, "- [ ] Task A\n- [ ] Task B\n"
        )
        # Add a shared file both workers will fight over.
        shared = project / "shared.txt"
        shared.write_text("base line\n")
        _git(project, "add", "-A")
        _git(project, "commit", "-m", "add shared")

        task_a, task_b = enumerate_tasks(folder)
        wt_a = worktree.create(project, folder, task_a.task_id)
        wt_b = worktree.create(project, folder, task_b.task_id)

        # Both modify the same line of shared.txt — guaranteed conflict.
        (wt_a / "shared.txt").write_text("A's line\n")
        sha_a = worktree.commit(wt_a, "agent: A clobbers shared")
        (wt_b / "shared.txt").write_text("B's line\n")
        sha_b = worktree.commit(wt_b, "agent: B clobbers shared")

        # A goes first cleanly.
        worktree.merge_back(wt_a, project)
        _git(project, "commit", "-C", sha_a)

        # B conflicts on shared.txt.
        with pytest.raises(worktree.MergeBackConflict) as exc:
            worktree.merge_back(wt_b, project)
        assert any("shared.txt" in p for p in exc.value.conflicted_paths)
        assert exc.value.sha == sha_b

        # Working tree was rolled back — no tracked changes staged or
        # unstaged. (`-uno` skips the untracked .ola/ worktrees directory.)
        status = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            cwd=str(project),
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert status.strip() == ""


class TestCleanup:
    def test_keep_on_failure_preserves(self, tmp_path):
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] One\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(project, folder, task.task_id)

        worktree.cleanup(wt, keep_on_failure=True)
        assert wt.exists()

    def test_removes_when_keep_false(self, tmp_path):
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] One\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(project, folder, task.task_id)

        worktree.cleanup(wt, keep_on_failure=False)
        assert not wt.exists()
        # And `git worktree list` no longer shows it.
        listing = subprocess.run(
            ["git", "worktree", "list"],
            cwd=str(project),
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert str(wt) not in listing

    def test_no_op_when_missing(self, tmp_path):
        worktree.cleanup(tmp_path / "does-not-exist", keep_on_failure=False)


class TestPruneBranch:
    def _branches(self, project: Path) -> str:
        return subprocess.run(
            ["git", "branch", "--list"],
            cwd=str(project),
            capture_output=True,
            check=True,
        ).stdout.decode()

    def test_deletes_the_task_branch(self, tmp_path):
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] One\n")
        task = enumerate_tasks(folder)[0]
        worktree.create(project, folder, task.task_id)
        worktree.cleanup(
            project / ".ola" / "worktrees" / task.task_id, keep_on_failure=False
        )

        # Before pruning the ref lingers; after, it is gone.
        assert f"ola/{folder.name}/{task.task_id}" in self._branches(project)
        worktree.prune_branch(project, folder, task.task_id)
        assert f"ola/{folder.name}/{task.task_id}" not in self._branches(project)

    def test_force_deletes_unmerged_branch(self, tmp_path):
        # A branch whose commit never landed on main (the rebase-recommit case,
        # patch-identical but not an ancestor) must still be pruned: -D, not -d.
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] One\n")
        task = enumerate_tasks(folder)[0]
        wt = worktree.create(project, folder, task.task_id)
        (wt / "stray.txt").write_text("never merged\n")
        worktree.commit(wt, "ola: stray work not on main")
        worktree.cleanup(wt, keep_on_failure=False)

        worktree.prune_branch(project, folder, task.task_id)
        assert f"ola/{folder.name}/{task.task_id}" not in self._branches(project)

    def test_no_op_when_branch_absent(self, tmp_path):
        # Never raises even if the branch was already cleared (best-effort).
        project, _agent_root, folder = _two_repos(tmp_path, "- [ ] One\n")
        task = enumerate_tasks(folder)[0]
        worktree.prune_branch(project, folder, task.task_id)
