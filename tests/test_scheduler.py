"""Tests for ola.scheduler — parallel run_folder against a stub agent."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

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


class _FailThenTickAgent(_TickingAgent):
    """Fails the first ``fail_times`` calls per task, then succeeds and ticks."""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self._fail_times = fail_times
        self._calls: dict[str, int] = {}

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        task_id = labels["task_id"]
        with self._lock:
            n = self._calls.get(task_id, 0) + 1
            self._calls[task_id] = n
        if n <= self._fail_times:
            return AgentResponse(output="boom", success=False, stats=IterationStats())
        return super().run(
            prompt, workdir, state_dir=state_dir, labels=labels, on_progress=on_progress
        )

    def version(self):
        return "0.0.0"


class _RateLimitedThenTicksAgent(_TickingAgent):
    """Returns rate_limited on the first call, then succeeds and ticks."""

    def __init__(self, resets_at) -> None:
        super().__init__()
        self._resets_at = resets_at
        self.calls = 0

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        self.calls += 1
        if self.calls == 1:
            stats = IterationStats(
                error_type="rate_limited",
                error_message="limit hit",
                rate_limit_resets_at=self._resets_at,
            )
            return AgentResponse(output="rate limited", success=False, stats=stats)
        return super().run(
            prompt, workdir, state_dir=state_dir, labels=labels, on_progress=on_progress
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


def test_run_folder_stagnant_exhausts_attempts_then_fails(tmp_path):
    """A stagnant agent is retried up to max_attempts, then the task stays failed."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Pretend success\n")
    task = enumerate_tasks(folder)[0]

    agent = _StagnantAgent()
    run_folder(agent, folder, repo, initial_cap=1, max_attempts=2)

    # Still unticked; merge_back never ran so no propagation commit landed.
    assert task_is_checked(folder, task.task_id) is False
    log = _log_oneline(repo, "main")
    assert len(log) == 2  # initial + folder-add

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert entry.attempts == 2  # initial attempt + one retry
    assert "stagnant" in entry.last_error.lower()


def test_run_folder_halts_after_consecutive_stagnant(tmp_path):
    """Five consecutive stagnant attempts halt the folder, sparing later tasks."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    plan = "".join(f"- [ ] Task {i}\n" for i in range(6))
    folder = _setup_folder(repo, "agent-folder", plan)
    tasks = enumerate_tasks(folder)

    agent = _StagnantAgent()
    # cap=1 keeps the stagnant attempts strictly sequential and deterministic.
    run_folder(agent, folder, repo, initial_cap=1, max_attempts=0)

    # The circuit breaker trips at the 5th consecutive stagnant attempt, so
    # only five tasks were ever dispatched; the sixth is left untouched.
    state = TaskState.load(folder)
    failed = [t for t in tasks if state.get(t.task_id).status == "failed"]
    pending = [t for t in tasks if state.get(t.task_id).status == "pending"]
    assert len(failed) == 5
    assert len(pending) == 1
    assert all("stagnant" in state.get(t.task_id).last_error.lower() for t in failed)

    # No propagation commits landed.
    assert len(_log_oneline(repo, "main")) == 2


def test_run_folder_stagnation_counter_resets_on_progress(tmp_path):
    """A real success between stagnant attempts resets the folder-wide counter.

    With stagnant tasks interleaved with successes, the consecutive count never
    reaches the threshold, so the folder runs to completion instead of halting.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    # 4 stagnant + 1 success + 4 stagnant: never 5 consecutive stagnant.
    plan = (
        "".join(f"- [ ] Stagnant {i}\n" for i in range(4))
        + "- [ ] Real work\n"
        + "".join(f"- [ ] Stagnant {i}\n" for i in range(4, 8))
    )
    folder = _setup_folder(repo, "agent-folder", plan)
    tasks = enumerate_tasks(folder)
    success_task = tasks[4]

    class _MixedAgent(_TickingAgent):
        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            if labels["task_id"] == success_task.task_id:
                return super().run(
                    prompt,
                    workdir,
                    state_dir=state_dir,
                    labels=labels,
                    on_progress=on_progress,
                )
            return AgentResponse(output="lied", success=True, stats=IterationStats())

    agent = _MixedAgent()
    run_folder(agent, folder, repo, initial_cap=1, max_attempts=0)

    # Every task was attempted (no halt): the success completed, the rest failed.
    state = TaskState.load(folder)
    assert state.get(success_task.task_id).status == "complete"
    stagnant = [t for t in tasks if t.task_id != success_task.task_id]
    assert all(state.get(t.task_id).status == "failed" for t in stagnant)


# --- max_attempts retries ---


def test_run_folder_retries_failed_task_then_succeeds(tmp_path):
    """With max_attempts, a task that fails once is requeued and then completes."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Flaky task\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailThenTickAgent(fail_times=1)
    run_folder(agent, folder, repo, initial_cap=1, max_attempts=2)

    # The retry succeeded: ticked on main, artefact landed, worktree gone.
    assert task_is_checked(folder, task.task_id) is True
    assert (repo / f"file_{task.task_id}.txt").read_text() == task.task_id
    assert not (folder / ".ola" / "worktrees" / task.task_id).exists()

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "complete"
    assert entry.attempts == 2  # one failed attempt + one successful retry
    assert entry.last_error is None


def test_run_folder_exhausts_max_attempts_then_fails(tmp_path):
    """A task that always fails is retried up to max_attempts, then stays failed."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Always fails\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailingAgent()
    run_folder(agent, folder, repo, initial_cap=1, max_attempts=2)

    # Tried twice total (initial attempt + one retry), never ticked.
    assert task_is_checked(folder, task.task_id) is False

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert entry.attempts == 2
    assert "boom" in entry.last_error

    # Final failed worktree retained for post-mortem; PLAN.md unchanged.
    assert (folder / ".ola" / "worktrees" / task.task_id).exists()
    log = _log_oneline(repo, "main")
    assert len(log) == 2  # initial + folder-add, no propagation


def test_run_folder_default_no_retries(tmp_path):
    """Default max_attempts=0 → a failing task is tried exactly once."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Always fails\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailThenTickAgent(fail_times=1)
    run_folder(agent, folder, repo, initial_cap=1)

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert entry.attempts == 1


# --- STATS.jsonl phase shape ---


def test_run_folder_writes_parallel_phase_stats(tmp_path):
    """Each attempt appends a STATS.jsonl row with phase ``task-<id>-<attempt>``."""
    import json

    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Task A\n- [ ] Task B\n")
    tasks = enumerate_tasks(folder)

    agent = _TickingAgent()
    run_folder(agent, folder, repo, initial_cap=2)

    stats_file = folder / "STATS.jsonl"
    assert stats_file.exists()
    records = [
        json.loads(line) for line in stats_file.read_text().splitlines() if line.strip()
    ]
    phases = {r["phase"] for r in records}
    assert phases == {f"task-{t.task_id}-1" for t in tasks}
    # The agent mnemonic/version are recorded alongside each row.
    assert all(r["agent"] == "stub" for r in records)
    # A successful tick registers a +1 completion delta for the worktree's plan.
    assert all(r["tasks_completed_delta"] == 1 for r in records)


def test_run_folder_failure_still_writes_stats(tmp_path):
    """A failed attempt still appends a stats row (delta 0, agent recorded)."""
    import json

    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Will fail\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailingAgent()
    run_folder(agent, folder, repo, initial_cap=1)

    records = [
        json.loads(line)
        for line in (folder / "STATS.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["phase"] == f"task-{task.task_id}-1"
    assert records[0]["tasks_completed_delta"] == 0


# --- Rate-limit sleep-and-resume (moved from the old inner loop) ---


def test_run_folder_rate_limit_sleeps_then_resumes(tmp_path):
    """A rate_limited response sleeps then re-runs the same task to completion."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Build the thing\n")
    task = enumerate_tasks(folder)[0]

    resets_at = int(time.time()) + 3
    agent = _RateLimitedThenTicksAgent(resets_at=resets_at)

    slept: list[float] = []
    with patch("ola.scheduler.time.sleep", side_effect=lambda d: slept.append(d)):
        run_folder(agent, folder, repo, initial_cap=1)

    # Slept once (does not burn a retry attempt), then resumed and completed.
    assert len(slept) == 1
    assert 3 <= slept[0] <= 15
    assert agent.calls == 2
    assert task_is_checked(folder, task.task_id) is True

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "complete"
    # Rate-limit resume is transient, so it counts as a single attempt.
    assert entry.attempts == 1


def test_run_folder_rate_limit_too_far_fails(tmp_path):
    """A reset beyond the wait cap fails the task without sleeping."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    folder = _setup_folder(repo, "agent-folder", "- [ ] Build the thing\n")
    task = enumerate_tasks(folder)[0]

    resets_at = int(time.time()) + 9 * 3600  # beyond the 8h cap
    agent = _RateLimitedThenTicksAgent(resets_at=resets_at)

    slept: list[float] = []
    with patch("ola.scheduler.time.sleep", side_effect=lambda d: slept.append(d)):
        run_folder(agent, folder, repo, initial_cap=1)

    assert slept == []  # never slept
    assert agent.calls == 1
    assert task_is_checked(folder, task.task_id) is False

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert "rate limit" in entry.last_error.lower()


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
