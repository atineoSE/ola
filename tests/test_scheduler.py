"""Tests for ola.scheduler — parallel run_folder against a stub agent."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.plan import enumerate_tasks, set_task_checked, task_is_checked
from ola.scheduler import (
    _DEFAULT_TASK_PROMPT,
    _load_task_prompt,
    _substitute,
    run_folder,
)
from ola.stats import IterationStats
from ola.taskstate import TaskState


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def _init_repo(repo: Path) -> None:
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


class _TickingAgent(Agent):
    """Stub agent that writes a unique file and ticks the assigned checkbox."""

    mnemonic = "stub"
    state_dir_name = ""

    def __init__(self) -> None:
        super().__init__()
        self.invocations: list[dict] = []
        self._lock = threading.Lock()

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        with self._lock:
            self.invocations.append(
                {
                    "labels": dict(labels or {}),
                    "workdir": workdir,
                    "state_dir": state_dir,
                }
            )
        task_id = labels["task_id"]
        folder_name = labels["folder"]
        worktree_folder = Path(workdir) / folder_name
        # Drop a unique artefact so we can verify propagation.
        (Path(workdir) / f"file_{task_id}.txt").write_text(task_id)
        # Tick this task's checkbox in the worktree's PLAN.md.
        set_task_checked(worktree_folder, task_id, True)
        return AgentResponse(output="ok", success=True, stats=IterationStats())

    def version(self):
        return "0.0.0"


class _FailingAgent(Agent):
    """Stub agent that returns success=False without touching the worktree."""

    mnemonic = "stub"
    state_dir_name = ""

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        return AgentResponse(output="boom", success=False, stats=IterationStats())

    def version(self):
        return "0.0.0"


class _StagnantAgent(Agent):
    """Stub agent that returns success=True but never ticks the checkbox."""

    mnemonic = "stub"
    state_dir_name = ""

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        return AgentResponse(
            output="silently lied", success=True, stats=IterationStats()
        )

    def version(self):
        return "0.0.0"


# --- Prompt helpers ---


def test_substitute_replaces_both_placeholders():
    out = _substitute("hi {{task_text}} / {{task_id}}", "build X", "t-abc")
    assert out == "hi build X / t-abc"


def test_load_task_prompt_falls_back_to_default(tmp_path):
    assert _load_task_prompt(tmp_path) == _DEFAULT_TASK_PROMPT


def test_load_task_prompt_prefers_folder_local(tmp_path):
    (tmp_path / "TASK-PROMPT.md").write_text("override: {{task_id}}")
    assert _load_task_prompt(tmp_path) == "override: {{task_id}}"


# --- run_folder happy path ---


def test_run_folder_single_task_success(tmp_path):
    """One task, agent ticks the checkbox → propagated to main with the agent's commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Build the thing\n")
    task = enumerate_tasks(folder)[0]

    agent = _TickingAgent()
    run_folder(agent, folder, repo, initial_cap=1)

    # PLAN.md on main is ticked.
    assert task_is_checked(folder, task.task_id) is True

    # Agent's artefact landed on main.
    assert (repo / f"file_{task.task_id}.txt").read_text() == task.task_id

    # A propagation commit landed on main (initial + folder-add + propagated).
    log = _log_oneline(repo, "main")
    assert len(log) == 3
    assert any(f"agent-folder {task.task_id}" in line for line in log)

    # Worktree was cleaned up.
    wt = folder / ".ola" / "worktrees" / task.task_id
    assert not wt.exists()

    # tasks.json reflects completion.
    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "complete"
    assert entry.attempts == 1
    assert entry.last_error is None

    # Agent was called with the expected labels.
    assert len(agent.invocations) == 1
    labels = agent.invocations[0]["labels"]
    assert labels == {
        "folder": "agent-folder",
        "task_id": task.task_id,
        "attempt": "1",
    }


def test_run_folder_three_tasks_all_complete(tmp_path):
    """Three independent tasks all complete; each ticked, three propagation commits."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(
        repo,
        "agent-folder",
        "- [ ] Task A\n- [ ] Task B\n- [ ] Task C\n",
    )
    tasks = enumerate_tasks(folder)

    agent = _TickingAgent()
    run_folder(agent, folder, repo, initial_cap=3)

    for t in tasks:
        assert task_is_checked(folder, t.task_id) is True
        assert (repo / f"file_{t.task_id}.txt").read_text() == t.task_id
        assert not (folder / ".ola" / "worktrees" / t.task_id).exists()

    # initial + folder-add + 3 propagations = 5 commits.
    log = _log_oneline(repo, "main")
    assert len(log) == 5

    state = TaskState.load(folder)
    assert all(state.get(t.task_id).status == "complete" for t in tasks)


def test_run_folder_skips_already_complete_tasks(tmp_path):
    """Tasks already ticked in PLAN.md are not re-run."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(
        repo,
        "agent-folder",
        "- [x] Already done\n- [ ] Still pending\n",
    )
    pending = [t for t in enumerate_tasks(folder) if not t.checked][0]

    agent = _TickingAgent()
    run_folder(agent, folder, repo, initial_cap=2)

    assert len(agent.invocations) == 1
    assert agent.invocations[0]["labels"]["task_id"] == pending.task_id


# --- run_folder failure paths ---


def test_run_folder_agent_failure_marks_failed_and_retains_worktree(tmp_path):
    """Agent returns success=False → task marked failed, worktree kept, no commit on main."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Will fail\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailingAgent()
    run_folder(agent, folder, repo, initial_cap=1)

    # Still unchecked on main.
    assert task_is_checked(folder, task.task_id) is False

    # Worktree retained.
    wt = folder / ".ola" / "worktrees" / task.task_id
    assert wt.exists()

    # State marked failed.
    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert entry.last_error is not None
    assert "boom" in entry.last_error

    # No propagation commit landed on main.
    log = _log_oneline(repo, "main")
    assert len(log) == 2  # initial + folder-add


def test_run_folder_stagnant_agent_marks_failed(tmp_path):
    """Agent returns success but does not tick → marked failed with stagnant message."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Pretend success\n")
    task = enumerate_tasks(folder)[0]

    agent = _StagnantAgent()
    run_folder(agent, folder, repo, initial_cap=1)

    # Still unchecked on main and on the worktree.
    assert task_is_checked(folder, task.task_id) is False

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert "stagnant" in entry.last_error.lower()

    # No propagation commit landed.
    log = _log_oneline(repo, "main")
    assert len(log) == 2


# --- Concurrency ---


def test_run_folder_respects_initial_cap(tmp_path):
    """With cap=1, two workers never overlap; with cap=2, they can."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(
        repo, "agent-folder", "- [ ] T1\n- [ ] T2\n- [ ] T3\n- [ ] T4\n"
    )

    # Custom agent that records concurrent overlap window.
    overlap: dict[str, int] = {"max": 0, "current": 0}
    lock = threading.Lock()

    class _SlowAgent(_TickingAgent):
        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            with lock:
                overlap["current"] += 1
                overlap["max"] = max(overlap["max"], overlap["current"])
            try:
                time.sleep(0.05)
                return super().run(
                    prompt,
                    workdir,
                    state_dir=state_dir,
                    labels=labels,
                    on_progress=on_progress,
                )
            finally:
                with lock:
                    overlap["current"] -= 1

    agent = _SlowAgent()
    run_folder(agent, folder, repo, initial_cap=1)
    assert overlap["max"] == 1

    # Reset and run again at cap=4. (Fresh folder to start clean.)
    repo2 = tmp_path / "repo2"
    _init_repo(repo2)
    folder2 = _setup_folder(
        repo2, "agent-folder", "- [ ] T1\n- [ ] T2\n- [ ] T3\n- [ ] T4\n"
    )
    overlap2: dict[str, int] = {"max": 0, "current": 0}
    lock2 = threading.Lock()

    class _SlowAgent2(_TickingAgent):
        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            with lock2:
                overlap2["current"] += 1
                overlap2["max"] = max(overlap2["max"], overlap2["current"])
            try:
                time.sleep(0.05)
                return super().run(
                    prompt,
                    workdir,
                    state_dir=state_dir,
                    labels=labels,
                    on_progress=on_progress,
                )
            finally:
                with lock2:
                    overlap2["current"] -= 1

    agent2 = _SlowAgent2()
    run_folder(agent2, folder2, repo2, initial_cap=4)
    assert overlap2["max"] >= 2


def test_run_folder_empty_plan_is_noop(tmp_path):
    """A folder with no pending tasks doesn't spawn any workers and doesn't commit."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [x] Already done\n")

    agent = _TickingAgent()
    run_folder(agent, folder, repo, initial_cap=2)

    assert agent.invocations == []
    log = _log_oneline(repo, "main")
    assert len(log) == 2  # initial + folder-add


# --- Prompt substitution end-to-end ---


def test_run_folder_passes_substituted_prompt(tmp_path):
    """The agent receives a prompt with {{task_text}} and {{task_id}} resolved."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Build the widget\n")
    task = enumerate_tasks(folder)[0]
    (folder / "TASK-PROMPT.md").write_text("Task: {{task_text}}; id: {{task_id}}")

    captured = {}

    class _CaptureAgent(_TickingAgent):
        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            captured["prompt"] = prompt
            return super().run(
                prompt,
                workdir,
                state_dir=state_dir,
                labels=labels,
                on_progress=on_progress,
            )

    agent = _CaptureAgent()
    run_folder(agent, folder, repo, initial_cap=1)

    assert "Task: Build the widget" in captured["prompt"]
    assert f"id: {task.task_id}" in captured["prompt"]
    # Absolute PLAN.md path is appended so the agent can find the file.
    assert "PLAN.md is located at:" in captured["prompt"]
