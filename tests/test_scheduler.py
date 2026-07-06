"""Tests for ola.scheduler — parallel run_folder against a stub agent.

The scheduler now spans two repos: per-task worktrees branch from the *project*
repo (the agent's code lands there via ``merge_back``), while the plan and its
checkbox ticks live in a separate *agent folder* repo. Helpers below build that
two-repo layout; the live PLAN.md is staged into ``<worktree>/.ola/PLAN.md`` for
the agent, so stub agents tick *that* copy.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ola.agents.base import Agent, AgentResponse
from ola.events.client import Emitter, LocalSink
from ola.plan import enumerate_tasks, set_task_checked, task_is_checked
from ola.scheduler import (
    DEFAULT_CONCURRENCY,
    _DEFAULT_TASK_PROMPT,
    RunInterrupted,
    _load_task_prompt,
    _run_probe,
    _substitute,
    append_metric_sample,
    read_concurrency,
    run_folder,
    write_concurrency,
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


def _two_repos(
    tmp_path: Path, plan: str, folder_name: str = "agent-folder"
) -> tuple[Path, Path, Path]:
    """Build (project_repo, agent_root, folder) for a two-repo scheduler run.

    *project_repo* is where per-task worktrees branch from and code lands;
    *agent_root* holds the numbered *folder* and its PLAN.md ticks.
    """
    project = tmp_path / "project"
    _init_repo(project)
    agent_root = tmp_path / "agent"
    _init_repo(agent_root)
    folder = _setup_folder(agent_root, folder_name, plan)
    return project, agent_root, folder


def _log_oneline(repo: Path, ref: str = "main") -> list[str]:
    out = subprocess.run(
        ["git", "log", "--oneline", ref],
        cwd=str(repo),
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [ln for ln in out.splitlines() if ln.strip()]


def _plan_copy(workdir) -> Path:
    """The staged PLAN.md copy the scheduler hands the agent in its worktree."""
    return Path(workdir) / ".ola"


class _TickingAgent(Agent):
    """Stub agent: writes a unique file and ticks the assigned checkbox.

    The file is written into the project worktree (it rides ``merge_back`` to the
    project repo); the tick goes into the staged ``.ola/PLAN.md`` copy.
    """

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
        # Drop a unique artefact in the project worktree so we can verify
        # propagation to the project repo.
        (Path(workdir) / f"file_{task_id}.txt").write_text(task_id)
        # Tick this task's checkbox in the staged PLAN.md copy.
        set_task_checked(_plan_copy(workdir), task_id, True)
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


class _SignalingAgent(Agent):
    """On first run, sends ``signum`` to this process, then blocks without ticking.

    Simulates a worker caught mid-flight: the signal reaches the scheduler's
    main-thread handler, and this run stays parked (so its task is still
    ``running``) until the test releases ``gate``.
    """

    mnemonic = "stub"
    state_dir_name = ""

    def __init__(self, signum: int) -> None:
        super().__init__()
        self._signum = signum
        self.gate = threading.Event()
        self.entered = threading.Event()

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        self.entered.set()
        os.kill(os.getpid(), self._signum)
        # Stay running until released so the scheduler reacts to the signal
        # while this task is still in flight.
        self.gate.wait(timeout=10)
        return AgentResponse(
            output="interrupted", success=False, stats=IterationStats()
        )

    def version(self):
        return "0.0.0"


# --- Prompt helpers ---


def test_substitute_replaces_placeholders():
    out = _substitute(
        "hi {{task_text}} / {{task_id}} @ {{plan_path}}",
        "build X",
        "t-abc",
        plan_path="/wt/.ola/PLAN.md",
    )
    assert out == "hi build X / t-abc @ /wt/.ola/PLAN.md"


def test_load_task_prompt_falls_back_to_default(tmp_path):
    assert _load_task_prompt(tmp_path) == _DEFAULT_TASK_PROMPT


def test_load_task_prompt_prefers_folder_local(tmp_path):
    (tmp_path / "TASK-PROMPT.md").write_text("override: {{task_id}}")
    assert _load_task_prompt(tmp_path) == "override: {{task_id}}"


# --- run_folder happy path ---


def test_run_folder_single_task_success(tmp_path):
    """One task, agent ticks the checkbox → code on project, tick on agent folder."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Build the thing\n")
    task = enumerate_tasks(folder)[0]

    agent = _TickingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1)

    # PLAN.md on the agent folder is ticked.
    assert task_is_checked(folder, task.task_id) is True

    # Agent's artefact landed on the project repo.
    assert (project / f"file_{task.task_id}.txt").read_text() == task.task_id

    # The project repo gained one code commit (initial + propagated).
    plog = _log_oneline(project, "main")
    assert len(plog) == 2
    assert any(f"agent-folder {task.task_id}" in line for line in plog)

    # The agent folder gained one tick commit (initial + folder-add + tick).
    alog = _log_oneline(agent_root, "main")
    assert len(alog) == 3
    assert any(f"agent-folder {task.task_id}" in line for line in alog)

    # Worktree (under the project repo) was cleaned up.
    wt = project / ".ola" / "worktrees" / task.task_id
    assert not wt.exists()

    # The per-task branch was pruned too — a completed run leaves no ola/* refs.
    branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=str(project),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert f"ola/{folder.name}/{task.task_id}" not in branches

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
    # The agent worked in the project worktree, not the agent folder.
    assert agent.invocations[0]["workdir"] == str(wt)


def test_run_folder_already_committed_tick_is_noop_not_crash(tmp_path):
    """A pre-committed checkbox must complete the task, not crash _propagate.

    Reproduces the un-jailed-agent failure: an agent that wanders out of its
    worktree into the live agent folder ticks PLAN.md there, so the box is
    already ticked *and committed* by the time _propagate runs. set_task_checked
    is then a no-op and nothing is staged — committing an empty index exits 1
    ("nothing to commit"). The agent-root commit must be guarded like the
    project commit so the task completes cleanly instead of raising.
    """
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Build the thing\n")
    task = enumerate_tasks(folder)[0]

    class _WandersIntoLiveFolderAgent(_TickingAgent):
        # Ticks its worktree copy (so it isn't stagnant) and *also* ticks and
        # commits PLAN.md in the live agent root, mimicking an agent that wasn't
        # confined to its worktree.
        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            resp = super().run(
                prompt, workdir, state_dir=state_dir, labels=labels,
                on_progress=on_progress,
            )
            set_task_checked(folder, labels["task_id"], True)
            _git(agent_root, "add", f"{folder.name}/PLAN.md")
            _git(agent_root, "commit", "-m", "agent wandered: pre-tick")
            return resp

    agent = _WandersIntoLiveFolderAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1)

    # The box is ticked and the task completes — no nothing-to-commit crash.
    assert task_is_checked(folder, task.task_id) is True
    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "complete"
    assert entry.last_error is None

    # The agent's code still propagated to the project repo.
    assert (project / f"file_{task.task_id}.txt").read_text() == task.task_id


def test_run_folder_warms_up_agent_before_dispatch(tmp_path):
    """warm_up() runs exactly once, on the main thread, before any task runs.

    Guards the litellm import-deadlock fix: the OpenHands backend's in-process
    imports must happen serially before workers spawn.
    """
    project, agent_root, folder = _two_repos(
        tmp_path, "- [ ] One\n- [ ] Two\n- [ ] Three\n"
    )

    class _WarmUpAgent(_TickingAgent):
        def __init__(self) -> None:
            super().__init__()
            self.warm_calls = 0
            self.warm_thread: str | None = None
            self.ran_before_warmup = False

        def warm_up(self) -> None:
            self.warm_calls += 1
            self.warm_thread = threading.current_thread().name
            # No task may have run before warm_up completes.
            if self.invocations:
                self.ran_before_warmup = True

    agent = _WarmUpAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=3)

    assert agent.warm_calls == 1
    assert agent.warm_thread == threading.main_thread().name
    assert agent.ran_before_warmup is False
    assert len(agent.invocations) == 3


def test_run_folder_three_tasks_all_complete(tmp_path):
    """Three independent tasks all complete; each ticked, three code commits."""
    project, agent_root, folder = _two_repos(
        tmp_path,
        "- [ ] Task A\n- [ ] Task B\n- [ ] Task C\n",
    )
    tasks = enumerate_tasks(folder)

    agent = _TickingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=3)

    for t in tasks:
        assert task_is_checked(folder, t.task_id) is True
        assert (project / f"file_{t.task_id}.txt").read_text() == t.task_id
        assert not (project / ".ola" / "worktrees" / t.task_id).exists()

    # project: initial + 3 code commits = 4.
    assert len(_log_oneline(project, "main")) == 4

    state = TaskState.load(folder)
    assert all(state.get(t.task_id).status == "complete" for t in tasks)


def test_run_folder_skips_already_complete_tasks(tmp_path):
    """Tasks already ticked in PLAN.md are not re-run."""
    project, agent_root, folder = _two_repos(
        tmp_path,
        "- [x] Already done\n- [ ] Still pending\n",
    )
    pending = [t for t in enumerate_tasks(folder) if not t.checked][0]

    agent = _TickingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=2)

    assert len(agent.invocations) == 1
    assert agent.invocations[0]["labels"]["task_id"] == pending.task_id


class _CommittingAgent(Agent):
    """Faithful stub: creates the named file, ticks its checkbox, and commits.

    Unlike :class:`_TickingAgent`, this agent makes its *own* commit in the
    worktree with a distinctive message (``feat: <task_text>``). That lets the
    integration test assert the agent's original commit message survives the
    propagation onto the project repo (``git commit -C <sha>``), rather than the
    synthetic message the scheduler falls back to when the worktree has
    uncommitted changes.
    """

    mnemonic = "stub"
    state_dir_name = ""

    def __init__(self) -> None:
        super().__init__()
        self.invocations: list[dict] = []
        self._lock = threading.Lock()

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        with self._lock:
            self.invocations.append({"labels": dict(labels or {})})
        task_id = labels["task_id"]
        workdir = Path(workdir)
        # Parse the human task text out of the substituted prompt.
        match = re.search(r"The task is: (.*?) \(task id", prompt)
        task_text = match.group(1)
        # The task text is "Create file A" → drop "A.txt" in the project worktree.
        letter = task_text.rsplit(" ", 1)[-1]
        (workdir / f"{letter}.txt").write_text(task_text)
        set_task_checked(_plan_copy(workdir), task_id, True)
        # Commit in the worktree with the agent's own message.
        subprocess.run(
            ["git", "add", "-A"], cwd=str(workdir), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", f"feat: {task_text}"],
            cwd=str(workdir),
            capture_output=True,
            check=True,
        )
        return AgentResponse(output="ok", success=True, stats=IterationStats())

    def version(self):
        return "0.0.0"


def test_run_folder_three_independent_tasks_integration(tmp_path):
    """End-to-end: three independent tasks at concurrency=3 land on the project.

    A stub agent creates file A/B/C, ticks its own checkbox, and commits with
    its own message. Asserts: all three complete, PLAN.md fully ticked on the
    agent folder, three commits carry the agents' *original* messages on the
    project repo, and all three worktrees are cleaned up.
    """
    project, agent_root, folder = _two_repos(
        tmp_path,
        "- [ ] Create file A\n- [ ] Create file B\n- [ ] Create file C\n",
    )
    tasks = enumerate_tasks(folder)

    agent = _CommittingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=3)

    # Each task completed in tasks.json.
    state = TaskState.load(folder)
    assert all(state.get(t.task_id).status == "complete" for t in tasks)

    # PLAN.md on the agent folder is fully ticked.
    assert all(task_is_checked(folder, t.task_id) for t in tasks)

    # Each expected artefact landed on the project repo.
    for letter in ("A", "B", "C"):
        assert (project / f"{letter}.txt").read_text() == f"Create file {letter}"

    # Three commits carrying the agents' original messages landed on the project
    # repo, on top of its initial commit.
    plog = _log_oneline(project, "main")
    messages = {ln.split(" ", 1)[1] for ln in plog}
    for letter in ("A", "B", "C"):
        assert f"feat: Create file {letter}" in messages
    assert len(plog) == 4  # initial + 3 propagated

    # All three worktrees were cleaned up.
    for t in tasks:
        assert not (project / ".ola" / "worktrees" / t.task_id).exists()


# --- run_folder failure paths ---


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_run_folder_flushes_in_flight_tasks_on_signal(tmp_path, signum):
    """A SIGINT/SIGTERM mid-run records in-flight tasks as interrupted.

    Without the signal handler a killed scheduler leaves the task frozen at
    ``running`` with no terminal event. We assert it instead lands as ``failed``
    in tasks.json and emits a terminal ``failed`` event carrying
    ``data.interrupted = true`` — and that the run raises RunInterrupted.
    """
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] do a thing\n")
    task = enumerate_tasks(folder)[0]
    events_path = folder / ".ola" / "events.jsonl"
    emitter = Emitter([LocalSink(events_path)])

    agent = _SignalingAgent(signum)
    try:
        # run_folder shuts the pool down with wait=False on interrupt, so it
        # raises while the worker is still parked on the gate — letting us
        # assert the flushed snapshot before the worker can re-mark its task.
        with pytest.raises(RunInterrupted) as excinfo:
            run_folder(
                agent, folder, agent_root, project, initial_cap=1, emitter=emitter
            )

        assert excinfo.value.signum == signum

        # tasks.json: the in-flight task is recorded as interrupted, not frozen.
        state = TaskState.load(folder)
        entry = state.get(task.task_id)
        assert entry.status == "failed"
        assert "interrupted" in (entry.last_error or "")
        assert signal.Signals(signum).name in (entry.last_error or "")

        # events.jsonl: a terminal failed event with the interrupted flag.
        emitter.close()
        events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        interrupted_failures = [
            e
            for e in events
            if e["status"] == "failed" and e["data"].get("interrupted") is True
        ]
        assert len(interrupted_failures) == 1
        assert interrupted_failures[0]["task_id"] == task.task_id
    finally:
        # Release the parked worker so the pool thread can exit.
        agent.gate.set()


def test_run_folder_restores_signal_handlers_after_clean_run(tmp_path):
    """run_folder leaves the prior SIGINT/SIGTERM handlers in place on exit."""
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    project, agent_root, folder = _two_repos(tmp_path, "- [ ] task\n")
    run_folder(_TickingAgent(), folder, agent_root, project, initial_cap=1)

    after = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    assert after == before


def test_run_folder_agent_failure_marks_failed_and_retains_worktree(tmp_path):
    """Agent returns success=False → task failed, worktree kept, no code commit."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Will fail\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1)

    # Still unchecked on the agent folder.
    assert task_is_checked(folder, task.task_id) is False

    # Worktree retained under the project repo.
    wt = project / ".ola" / "worktrees" / task.task_id
    assert wt.exists()

    # State marked failed.
    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert entry.last_error is not None
    assert "boom" in entry.last_error

    # No code commit landed on the project repo (just its initial commit).
    assert len(_log_oneline(project, "main")) == 1


def test_run_folder_stagnant_agent_marks_failed(tmp_path):
    """Agent returns success but does not tick → failed with stagnant message."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Pretend success\n")
    task = enumerate_tasks(folder)[0]

    agent = _StagnantAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1)

    # Still unchecked on the agent folder.
    assert task_is_checked(folder, task.task_id) is False

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert "stagnant" in entry.last_error.lower()

    # No code commit landed.
    assert len(_log_oneline(project, "main")) == 1


def test_run_folder_stagnant_exhausts_attempts_then_fails(tmp_path):
    """A stagnant agent is retried up to max_attempts, then the task stays failed."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Pretend success\n")
    task = enumerate_tasks(folder)[0]

    agent = _StagnantAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1, max_attempts=2)

    # Still unticked; merge_back never ran so no code commit landed.
    assert task_is_checked(folder, task.task_id) is False
    assert len(_log_oneline(project, "main")) == 1

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert entry.attempts == 2  # initial attempt + one retry
    assert "stagnant" in entry.last_error.lower()


def test_run_folder_halts_after_consecutive_stagnant(tmp_path):
    """Five consecutive stagnant attempts halt the folder, sparing later tasks."""
    plan = "".join(f"- [ ] Task {i}\n" for i in range(6))
    project, agent_root, folder = _two_repos(tmp_path, plan)
    tasks = enumerate_tasks(folder)

    agent = _StagnantAgent()
    # cap=1 keeps the stagnant attempts strictly sequential and deterministic.
    run_folder(agent, folder, agent_root, project, initial_cap=1, max_attempts=0)

    # The circuit breaker trips at the 5th consecutive stagnant attempt, so
    # only five tasks were ever dispatched; the sixth is left untouched.
    state = TaskState.load(folder)
    failed = [t for t in tasks if state.get(t.task_id).status == "failed"]
    pending = [t for t in tasks if state.get(t.task_id).status == "pending"]
    assert len(failed) == 5
    assert len(pending) == 1
    assert all("stagnant" in state.get(t.task_id).last_error.lower() for t in failed)

    # No code commits landed.
    assert len(_log_oneline(project, "main")) == 1


def test_run_folder_stagnation_counter_resets_on_progress(tmp_path):
    """A real success between stagnant attempts resets the folder-wide counter.

    With stagnant tasks interleaved with successes, the consecutive count never
    reaches the threshold, so the folder runs to completion instead of halting.
    """
    # 4 stagnant + 1 success + 4 stagnant: never 5 consecutive stagnant.
    plan = (
        "".join(f"- [ ] Stagnant {i}\n" for i in range(4))
        + "- [ ] Real work\n"
        + "".join(f"- [ ] Stagnant {i}\n" for i in range(4, 8))
    )
    project, agent_root, folder = _two_repos(tmp_path, plan)
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
    run_folder(agent, folder, agent_root, project, initial_cap=1, max_attempts=0)

    # Every task was attempted (no halt): the success completed, the rest failed.
    state = TaskState.load(folder)
    assert state.get(success_task.task_id).status == "complete"
    stagnant = [t for t in tasks if t.task_id != success_task.task_id]
    assert all(state.get(t.task_id).status == "failed" for t in stagnant)


# --- max_attempts retries ---


def test_run_folder_retries_failed_task_then_succeeds(tmp_path):
    """With max_attempts, a task that fails once is requeued and then completes."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Flaky task\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailThenTickAgent(fail_times=1)
    run_folder(agent, folder, agent_root, project, initial_cap=1, max_attempts=2)

    # The retry succeeded: ticked on the agent folder, artefact on the project,
    # worktree gone.
    assert task_is_checked(folder, task.task_id) is True
    assert (project / f"file_{task.task_id}.txt").read_text() == task.task_id
    assert not (project / ".ola" / "worktrees" / task.task_id).exists()

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "complete"
    assert entry.attempts == 2  # one failed attempt + one successful retry
    assert entry.last_error is None


def test_run_folder_exhausts_max_attempts_then_fails(tmp_path):
    """A task that always fails is retried up to max_attempts, then stays failed."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Always fails\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1, max_attempts=2)

    # Tried twice total (initial attempt + one retry), never ticked.
    assert task_is_checked(folder, task.task_id) is False

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert entry.attempts == 2
    assert "boom" in entry.last_error

    # Final failed worktree retained for post-mortem; no code commit landed.
    assert (project / ".ola" / "worktrees" / task.task_id).exists()
    assert len(_log_oneline(project, "main")) == 1


class _ConflictOnceAgent(_TickingAgent):
    """First attempt collides on merge-back; the retry lands a disjoint file.

    On attempt 1 this worktree edits ``shared.txt`` while a *rival* lands a
    conflicting edit on the same line of the project HEAD mid-flight, so the
    merge-back 3-way collides. The retry — re-branched by ``create`` off the
    now-updated HEAD — instead writes a unique file that merges cleanly.
    """

    def __init__(self, project_repo: Path) -> None:
        super().__init__()
        self._project = project_repo
        self.attempts_seen: list[int] = []

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        task_id = labels["task_id"]
        attempt = int(labels["attempt"])
        with self._lock:
            self.attempts_seen.append(attempt)
        wt = Path(workdir)
        if attempt == 1:
            (wt / "shared.txt").write_text("worker line\n")
            (self._project / "shared.txt").write_text("rival line\n")
            _git(self._project, "add", "shared.txt")
            _git(self._project, "commit", "-m", "rival landed mid-flight")
        else:
            (wt / f"file_{task_id}.txt").write_text("clean")
        set_task_checked(_plan_copy(workdir), task_id, True)
        return AgentResponse(output="ok", success=True, stats=IterationStats())

    def version(self):
        return "0.0.0"


def test_run_folder_merge_conflict_retries_then_lands(tmp_path):
    """A merge-back conflict is a non-stagnant failed attempt: the task is
    requeued and the retry, re-branched off the updated project HEAD, lands."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Edit shared\n")
    (project / "shared.txt").write_text("base\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "add shared")
    task = enumerate_tasks(folder)[0]

    agent = _ConflictOnceAgent(project)
    run_folder(agent, folder, agent_root, project, initial_cap=1, max_attempts=2)

    # Completed on the second attempt, ticked on the agent folder.
    assert task_is_checked(folder, task.task_id) is True
    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "complete"
    assert entry.attempts == 2
    assert agent.attempts_seen == [1, 2]

    # The rival's content survived and the retry's disjoint file landed on top.
    assert (project / "shared.txt").read_text() == "rival line\n"
    assert (project / f"file_{task.task_id}.txt").read_text() == "clean"


class _AlwaysConflictAgent(_TickingAgent):
    """Every attempt collides on merge-back (a rival keeps editing the line)."""

    def __init__(self, project_repo: Path) -> None:
        super().__init__()
        self._project = project_repo
        self.calls = 0

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        task_id = labels["task_id"]
        with self._lock:
            self.calls += 1
            n = self.calls
        wt = Path(workdir)
        (wt / "shared.txt").write_text(f"worker {n}\n")
        (self._project / "shared.txt").write_text(f"rival {n}\n")
        _git(self._project, "add", "shared.txt")
        _git(self._project, "commit", "-m", f"rival {n}")
        set_task_checked(_plan_copy(workdir), task_id, True)
        return AgentResponse(output="ok", success=True, stats=IterationStats())

    def version(self):
        return "0.0.0"


def test_run_folder_merge_conflict_escalates_after_attempts(tmp_path):
    """A merge-back conflict that survives the whole --max-attempts budget is
    recorded as blocked, so the existing janitor escalation can relocate it —
    durable coupling, not stagnation."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Edit shared\n")
    (project / "shared.txt").write_text("base\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "add shared")
    task = enumerate_tasks(folder)[0]

    agent = _AlwaysConflictAgent(project)
    # janitor_enabled=False keeps the escalation to its terminal state (blocked
    # + marker) without spawning a real janitor agent.
    run_folder(
        agent,
        folder,
        agent_root,
        project,
        initial_cap=1,
        max_attempts=2,
        janitor_enabled=False,
    )

    # Initial attempt + one retry, then escalation (no third dispatch).
    assert agent.calls == 2
    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "blocked"
    assert entry.attempts == 2
    assert "merge-back conflict" in entry.last_error

    # The blocked marker is recorded for the janitor / human audit, and the task
    # was never ticked.
    assert (folder / ".ola" / "blocked" / f"{task.task_id}.reason").exists()
    assert task_is_checked(folder, task.task_id) is False


def test_run_folder_materializes_default_concurrency(tmp_path):
    """A run with no .ola/concurrency writes the default so it is auditable."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Task A\n")
    assert not (folder / ".ola" / "concurrency").exists()

    run_folder(
        _TickingAgent(), folder, agent_root, project, initial_cap=DEFAULT_CONCURRENCY
    )

    assert read_concurrency(folder) == DEFAULT_CONCURRENCY


def test_run_folder_does_not_clobber_existing_concurrency(tmp_path):
    """A pre-existing cap (e.g. set by the dashboard slider) is left untouched."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Task A\n")
    write_concurrency(folder, 5)

    run_folder(
        _TickingAgent(), folder, agent_root, project, initial_cap=DEFAULT_CONCURRENCY
    )

    assert read_concurrency(folder) == 5


def test_run_folder_default_no_retries(tmp_path):
    """Default max_attempts=0 → a failing task is tried exactly once."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Always fails\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailThenTickAgent(fail_times=1)
    run_folder(agent, folder, agent_root, project, initial_cap=1)

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert entry.attempts == 1


# --- STATS.jsonl phase shape ---


def test_run_folder_writes_parallel_phase_stats(tmp_path):
    """Each attempt appends a STATS.jsonl row with phase ``task-<id>-<attempt>``."""
    import json

    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Task A\n- [ ] Task B\n")
    tasks = enumerate_tasks(folder)

    agent = _TickingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=2)

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

    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Will fail\n")
    task = enumerate_tasks(folder)[0]

    agent = _FailingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1)

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
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Build the thing\n")
    task = enumerate_tasks(folder)[0]

    resets_at = int(time.time()) + 3
    agent = _RateLimitedThenTicksAgent(resets_at=resets_at)

    slept: list[float] = []
    with patch("ola.scheduler.time.sleep", side_effect=lambda d: slept.append(d)):
        run_folder(agent, folder, agent_root, project, initial_cap=1)

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
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Build the thing\n")
    task = enumerate_tasks(folder)[0]

    resets_at = int(time.time()) + 9 * 3600  # beyond the 8h cap
    agent = _RateLimitedThenTicksAgent(resets_at=resets_at)

    slept: list[float] = []
    with patch("ola.scheduler.time.sleep", side_effect=lambda d: slept.append(d)):
        run_folder(agent, folder, agent_root, project, initial_cap=1)

    assert slept == []  # never slept
    assert agent.calls == 1
    assert task_is_checked(folder, task.task_id) is False

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "failed"
    assert "rate limit" in entry.last_error.lower()


# --- Concurrency ---


def test_run_folder_respects_initial_cap(tmp_path):
    """With cap=1, two workers never overlap; with cap=2+, they can."""
    project, agent_root, folder = _two_repos(
        tmp_path, "- [ ] T1\n- [ ] T2\n- [ ] T3\n- [ ] T4\n"
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
    run_folder(agent, folder, agent_root, project, initial_cap=1)
    assert overlap["max"] == 1

    # Reset and run again at cap=4. (Fresh repos to start clean.)
    project2 = tmp_path / "project2"
    _init_repo(project2)
    agent_root2 = tmp_path / "agent2"
    _init_repo(agent_root2)
    folder2 = _setup_folder(
        agent_root2, "agent-folder", "- [ ] T1\n- [ ] T2\n- [ ] T3\n- [ ] T4\n"
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
    run_folder(agent2, folder2, agent_root2, project2, initial_cap=4)
    assert overlap2["max"] >= 2


def _wait_until(predicate, timeout=5.0, interval=0.02):
    """Poll *predicate* until true or *timeout* elapses; return its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_run_folder_honours_live_cap_increase(tmp_path):
    """Raising .ola/concurrency mid-run spawns more workers within a tick.

    Starts at cap 1 with workers that block until released, asserts exactly one
    runs, bumps the file to 3, and asserts the running count climbs to 3 (which
    requires the pool to grow past its initial max_workers=1). Then releases the
    workers and confirms the folder completes.
    """
    project, agent_root, folder = _two_repos(
        tmp_path, "".join(f"- [ ] T{i}\n" for i in range(6))
    )
    cap_file = folder / ".ola" / "concurrency"
    cap_file.parent.mkdir(parents=True, exist_ok=True)
    cap_file.write_text("1")

    release = threading.Event()
    counters = {"running": 0, "max": 0}
    lock = threading.Lock()

    class _BlockingAgent(_TickingAgent):
        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            with lock:
                counters["running"] += 1
                counters["max"] = max(counters["max"], counters["running"])
            try:
                release.wait(timeout=30)
                return super().run(
                    prompt,
                    workdir,
                    state_dir=state_dir,
                    labels=labels,
                    on_progress=on_progress,
                )
            finally:
                with lock:
                    counters["running"] -= 1

    agent = _BlockingAgent()
    worker = threading.Thread(
        target=run_folder,
        args=(agent, folder, agent_root, project),
        kwargs={"initial_cap": 1},
    )
    worker.start()
    try:
        # Exactly one worker starts under cap 1, and it stays at one.
        assert _wait_until(lambda: counters["running"] == 1)
        time.sleep(0.3)
        with lock:
            assert counters["max"] == 1

        # Bump the live cap; within a tick the running count tracks it to 3.
        cap_file.write_text("3")
        assert _wait_until(lambda: counters["running"] == 3, timeout=5.0)

        # Release the blocked workers; the folder drains to completion.
        release.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        release.set()
        worker.join(timeout=30)

    tstate = TaskState.load(folder)
    assert all(
        tstate.get(t.task_id).status == "complete" for t in enumerate_tasks(folder)
    )


# --- heartbeat / liveness ---


def _read_heartbeat(folder: Path) -> dict:
    return json.loads((folder / ".ola" / "heartbeat.json").read_text())


def test_run_folder_writes_heartbeat_on_clean_run(tmp_path):
    """A completed run leaves a heartbeat with the terminal (drained) snapshot."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] one\n- [ ] two\n")

    run_folder(_TickingAgent(), folder, agent_root, project, initial_cap=2)

    hb = _read_heartbeat(folder)
    # Terminal beat: everything drained, nothing in flight or pending.
    assert hb["running"] == 0
    assert hb["pending"] == 0
    assert hb["in_flight"] == []
    assert hb["folder"] == folder.name
    assert hb["cap"] == 2
    # ts is the documented ISO-8601 millisecond-Z shape.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", hb["ts"])


def test_run_folder_heartbeat_reflects_in_flight_tasks(tmp_path):
    """While workers are mid-flight the heartbeat names them; the final beat clears.

    This is the post-mortem signal the original stall lacked: a durable, on-disk
    record of which tasks were live the instant the scheduler last ticked.
    """
    project, agent_root, folder = _two_repos(
        tmp_path, "".join(f"- [ ] T{i}\n" for i in range(3))
    )

    release = threading.Event()

    class _BlockingAgent(_TickingAgent):
        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            release.wait(timeout=30)
            return super().run(
                prompt,
                workdir,
                state_dir=state_dir,
                labels=labels,
                on_progress=on_progress,
            )

    agent = _BlockingAgent()
    worker = threading.Thread(
        target=run_folder,
        args=(agent, folder, agent_root, project),
        kwargs={"initial_cap": 2},
    )
    worker.start()
    try:
        # Two workers block under cap 2; the heartbeat names exactly those two.
        def _two_in_flight() -> bool:
            try:
                return _read_heartbeat(folder)["running"] == 2
            except (FileNotFoundError, json.JSONDecodeError):
                return False

        assert _wait_until(_two_in_flight)
        hb = _read_heartbeat(folder)
        assert hb["pending"] == 1  # third task not yet dispatched
        ids = {entry["task_id"] for entry in hb["in_flight"]}
        running_ids = {
            e.task_id for e in TaskState.load(folder).all() if e.status == "running"
        }
        assert ids == running_ids
        assert all("elapsed_s" in entry for entry in hb["in_flight"])
    finally:
        release.set()
        worker.join(timeout=30)

    # Final forced beat after the loop drains: nothing left in flight.
    assert _read_heartbeat(folder)["running"] == 0


def test_run_folder_empty_plan_is_noop(tmp_path):
    """A folder with no pending tasks doesn't spawn any workers and doesn't commit."""
    project, agent_root, folder = _two_repos(tmp_path, "- [x] Already done\n")

    agent = _TickingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=2)

    assert agent.invocations == []
    # No code commits on the project repo (just its initial commit).
    assert len(_log_oneline(project, "main")) == 1


# --- Prompt substitution end-to-end ---


def test_run_folder_passes_substituted_prompt(tmp_path):
    """The agent receives a prompt with task/id/plan_path placeholders resolved."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Build the widget\n")
    task = enumerate_tasks(folder)[0]
    (folder / "TASK-PROMPT.md").write_text(
        "Task: {{task_text}}; id: {{task_id}}; plan: {{plan_path}}"
    )

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
    run_folder(agent, folder, agent_root, project, initial_cap=1)

    assert "Task: Build the widget" in captured["prompt"]
    assert f"id: {task.task_id}" in captured["prompt"]
    # {{plan_path}} is the absolute path of the staged PLAN.md copy in the
    # project worktree's .ola/.
    expected = str(project / ".ola" / "worktrees" / task.task_id / ".ola" / "PLAN.md")
    assert f"plan: {expected}" in captured["prompt"]


# --- Event emission (Phase 6) ---


class _ProgressTickingAgent(_TickingAgent):
    """Ticking agent that also pings ``on_progress`` so ``working`` events fire.

    Passes a throughput ``metrics`` block on the progress ping and returns
    non-zero token/decode stats, so terminal events carry ``metrics`` too.
    """

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        if on_progress is not None:
            on_progress(
                "doing the work",
                {"output_tokens": 10, "decode_ms": 500, "tokens_per_sec": 20.0},
            )
        response = super().run(
            prompt, workdir, state_dir=state_dir, labels=labels, on_progress=on_progress
        )
        response.stats = IterationStats(output_tokens=100, decode_ms=2000)
        return response


class _ProgressFailingAgent(Agent):
    """Pings ``on_progress`` then returns success=False (drives a ``failed`` event)."""

    mnemonic = "stub"
    state_dir_name = ""

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        if on_progress is not None:
            on_progress("about to fail")
        return AgentResponse(output="boom", success=False, stats=IterationStats())

    def version(self):
        return "0.0.0"


def test_run_folder_emits_all_event_types(tmp_path):
    """A trivial parallel run writes started/working/complete/failed to events.jsonl."""
    import json

    project, agent_root, folder = _two_repos(
        tmp_path, "- [ ] Will pass\n- [ ] Will fail\n"
    )
    tasks = enumerate_tasks(folder)

    class _RoutingAgent(_ProgressTickingAgent):
        """Succeeds on the first task, fails on the second."""

        def __init__(self, fail_task_id):
            super().__init__()
            self._fail_task_id = fail_task_id
            self._fail = _ProgressFailingAgent()

        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            if labels["task_id"] == self._fail_task_id:
                return self._fail.run(
                    prompt,
                    workdir,
                    state_dir=state_dir,
                    labels=labels,
                    on_progress=on_progress,
                )
            return super().run(
                prompt,
                workdir,
                state_dir=state_dir,
                labels=labels,
                on_progress=on_progress,
            )

    events_path = folder / ".ola" / "events.jsonl"
    emitter = Emitter([LocalSink(events_path)])
    agent = _RoutingAgent(fail_task_id=tasks[1].task_id)
    run_folder(agent, folder, agent_root, project, initial_cap=2, emitter=emitter)
    emitter.close()  # flush the LocalSink writer thread

    records = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]
    statuses = {r["status"] for r in records}
    assert {"started", "working", "complete", "failed"} <= statuses

    # Every envelope carries the fields the schema requires.
    for r in records:
        assert r["folder"] == "agent-folder"
        assert r["agent_backend"] == "stub"
        assert r["task_id"] in {t.task_id for t in tasks}

    # seq is monotonic from 0 per (agent_id, attempt).
    by_agent: dict[tuple, list[int]] = {}
    for r in records:
        by_agent.setdefault((r["agent_id"], r["attempt"]), []).append(r["seq"])
    for seqs in by_agent.values():
        assert seqs == list(range(len(seqs)))

    # working events carry the message plus the metrics block the agent passed.
    working = [r for r in records if r["status"] == "working"]
    assert working
    with_metrics = [r for r in working if "metrics" in r["data"]]
    assert with_metrics
    assert with_metrics[0]["data"]["metrics"]["output_tokens"] == 10

    # complete events carry the final metrics derived from response.stats
    # (100 tokens over 2000ms decode → 50.0 tok/s).
    complete = [r for r in records if r["status"] == "complete"]
    assert complete
    for r in complete:
        assert r["data"]["metrics"] == {
            "output_tokens": 100,
            "decode_ms": 2000,
            "tokens_per_sec": 50.0,
        }

    # failed events carry the error; the failing stub reports no throughput
    # stats, so the optional metrics block is omitted rather than zeroed.
    failed = [r for r in records if r["status"] == "failed"]
    assert failed
    for r in failed:
        assert r["data"]["error"]
        assert "metrics" not in r["data"]


# --- read_concurrency tests ---


def test_read_concurrency_missing_file_defaults(tmp_path):
    """No .ola/concurrency file → default, silently."""
    assert read_concurrency(tmp_path) == DEFAULT_CONCURRENCY
    assert read_concurrency(tmp_path, default=4) == 4


def test_read_concurrency_reads_positive_integer(tmp_path):
    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("5\n")
    assert read_concurrency(tmp_path) == 5


def test_read_concurrency_zero_is_valid_pause(tmp_path):
    """Cap of 0 ('pause new starts') is a valid value, not malformed."""
    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("0")
    assert read_concurrency(tmp_path) == 0


def test_read_concurrency_negative_rejected(tmp_path):
    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("-3")
    assert read_concurrency(tmp_path, default=2) == 2


def test_read_concurrency_malformed_defaults(tmp_path):
    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("not a number")
    assert read_concurrency(tmp_path, default=2) == 2


def test_read_concurrency_malformed_logs_warning(tmp_path, caplog):
    import logging

    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("abc")
    with caplog.at_level(logging.WARNING, logger="ola.scheduler"):
        read_concurrency(tmp_path)
    assert any("Malformed concurrency cap" in r.message for r in caplog.records)


def test_read_concurrency_negative_logs_warning(tmp_path, caplog):
    import logging

    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("-1")
    with caplog.at_level(logging.WARNING, logger="ola.scheduler"):
        read_concurrency(tmp_path)
    assert any("Negative concurrency cap" in r.message for r in caplog.records)


# --- write_concurrency tests ---


def test_write_concurrency_round_trips(tmp_path):
    write_concurrency(tmp_path, 5)
    assert (tmp_path / ".ola" / "concurrency").exists()
    assert read_concurrency(tmp_path) == 5


def test_write_concurrency_creates_ola_dir(tmp_path):
    """The .ola/ sidecar is created on demand (dashboard may write first)."""
    assert not (tmp_path / ".ola").exists()
    write_concurrency(tmp_path, 3)
    assert read_concurrency(tmp_path) == 3


def test_write_concurrency_zero_is_valid_pause(tmp_path):
    write_concurrency(tmp_path, 0)
    assert read_concurrency(tmp_path) == 0


def test_write_concurrency_overwrites(tmp_path):
    write_concurrency(tmp_path, 8)
    write_concurrency(tmp_path, 2)
    assert read_concurrency(tmp_path) == 2


def test_write_concurrency_negative_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        write_concurrency(tmp_path, -1)


# --- Blocked tasks + janitor ---


class _BlockingAgent(Agent):
    """Stub agent that runs the provisioned ola-blocked script and stops."""

    mnemonic = "stub"
    state_dir_name = ""

    def __init__(self, reason: str = "missing API key") -> None:
        super().__init__()
        self.reason = reason
        self.calls = 0
        self._lock = threading.Lock()

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        with self._lock:
            self.calls += 1
        script = Path(workdir) / ".ola" / "bin" / "ola-blocked"
        subprocess.run(
            [str(script), "--reason", self.reason], capture_output=True, check=True
        )
        return AgentResponse(
            output="blocked myself", success=True, stats=IterationStats()
        )

    def version(self):
        return "0.0.0"


def test_run_folder_blocked_task_is_terminal_no_retry(tmp_path):
    """A blocked task is marked blocked once and never retried, even with retries left."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Needs a secret\n")
    task = enumerate_tasks(folder)[0]

    agent = _BlockingAgent(reason="FOO_API_KEY is not available")
    run_folder(
        agent,
        folder,
        agent_root,
        project,
        initial_cap=1,
        max_attempts=3,
        janitor_enabled=False,
    )

    # Dispatched exactly once despite max_attempts=3.
    assert agent.calls == 1

    state = TaskState.load(folder)
    entry = state.get(task.task_id)
    assert entry.status == "blocked"
    assert entry.attempts == 1
    assert entry.last_error == "blocked: FOO_API_KEY is not available"

    # Unticked, no code commit, and the worktree was cleaned up (the reason is
    # recorded; nothing in the worktree is worth a post-mortem).
    assert task_is_checked(folder, task.task_id) is False
    assert len(_log_oneline(project, "main")) == 1
    assert not (project / ".ola" / "worktrees" / task.task_id).exists()

    # The branch shares the worktree's fate: with the worktree gone and the task
    # terminal (relocated by the janitor to a differently-named folder, so no
    # future create() reclaims it), the branch is pruned — no dangling ola/* ref.
    branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"ola/{folder.name}/{task.task_id}" not in branches

    # The marker is retained as the audit record (in the agent folder).
    assert (folder / ".ola" / "blocked" / f"{task.task_id}.reason").exists()


def test_run_folder_blocked_emits_failed_event_with_blocked_flag(tmp_path):
    import json

    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Needs a secret\n")

    events_path = folder / ".ola" / "events.jsonl"
    emitter = Emitter([LocalSink(events_path)])
    agent = _BlockingAgent(reason="no key")
    run_folder(
        agent,
        folder,
        agent_root,
        project,
        initial_cap=1,
        emitter=emitter,
        janitor_enabled=False,
    )
    emitter.close()

    records = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]
    failed = [r for r in records if r["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["blocked"] is True
    assert failed[0]["data"]["error"] == "blocked: no key"


def test_run_folder_blocked_does_not_trip_stagnation_breaker(tmp_path):
    """Six consecutive blocked tasks are signal, not stagnation: no halt."""
    plan = "".join(f"- [ ] Task {i}\n" for i in range(6))
    project, agent_root, folder = _two_repos(tmp_path, plan)
    tasks = enumerate_tasks(folder)

    agent = _BlockingAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1, janitor_enabled=False)

    # All six were dispatched (a stagnation halt would have stopped at five).
    state = TaskState.load(folder)
    assert all(state.get(t.task_id).status == "blocked" for t in tasks)


def test_run_folder_tick_beats_blocked_marker(tmp_path):
    """A task that ticks its checkbox completes even if it also ran ola-blocked."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Confused task\n")
    task = enumerate_tasks(folder)[0]

    class _TickAndBlockAgent(_TickingAgent):
        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            script = Path(workdir) / ".ola" / "bin" / "ola-blocked"
            subprocess.run(
                [str(script), "--reason", "second thoughts"],
                capture_output=True,
                check=True,
            )
            return super().run(
                prompt,
                workdir,
                state_dir=state_dir,
                labels=labels,
                on_progress=on_progress,
            )

    agent = _TickAndBlockAgent()
    run_folder(agent, folder, agent_root, project, initial_cap=1, janitor_enabled=False)

    state = TaskState.load(folder)
    assert state.get(task.task_id).status == "complete"
    assert task_is_checked(folder, task.task_id) is True
    # The stray marker was cleared — checkbox is truth.
    assert not (folder / ".ola" / "blocked" / f"{task.task_id}.reason").exists()


class _BlockThenJanitorAgent(_TickingAgent):
    """Blocks one task, then unblocks it when called in the janitor role.

    As the janitor it does what JANITOR-PROMPT mandates for outcome A:
    appends a prerequisite checkbox to the live PLAN.md, removes the blocked
    line, and creates the leftovers sibling folder named in the prompt.
    """

    def __init__(self, block_task_id: str, block_text: str) -> None:
        super().__init__()
        self._block_task_id = block_task_id
        self._block_text = block_text
        self.janitor_invocations: list[dict] = []

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        labels = labels or {}
        if labels.get("phase") == "janitor":
            with self._lock:
                self.janitor_invocations.append({"labels": dict(labels)})
            root = Path(workdir)
            folder = root / labels["folder"]
            plan = folder / "PLAN.md"
            lines = [
                ln for ln in plan.read_text().splitlines() if self._block_text not in ln
            ]
            lines.append("- [ ] Provision the secret")
            plan.write_text("\n".join(lines) + "\n")
            leftovers_name = re.search(
                r"named exactly `([^`]+-leftovers)`", prompt
            ).group(1)
            leftovers = root / leftovers_name
            leftovers.mkdir()
            (leftovers / "PLAN.md").write_text(
                "Blocked earlier (missing secret); prerequisites assumed done.\n\n"
                f"- [ ] {self._block_text}\n"
            )
            return AgentResponse(
                output="unblocked", success=True, stats=IterationStats()
            )
        if labels.get("task_id") == self._block_task_id:
            script = Path(workdir) / ".ola" / "bin" / "ola-blocked"
            subprocess.run(
                [str(script), "--reason", "missing secret"],
                capture_output=True,
                check=True,
            )
            return AgentResponse(output="blocked", success=True, stats=IterationStats())
        return super().run(
            prompt, workdir, state_dir=state_dir, labels=labels, on_progress=on_progress
        )


def test_run_folder_blocked_dispatches_janitor_and_runs_prereq(tmp_path):
    """The janitor's prerequisite task is picked up in the same folder run."""
    project, agent_root, folder = _two_repos(
        tmp_path, "- [ ] Call the FOO API\n", folder_name="01-api"
    )
    blocked = enumerate_tasks(folder)[0]

    agent = _BlockThenJanitorAgent(blocked.task_id, "Call the FOO API")
    run_folder(agent, folder, agent_root, project, initial_cap=1)

    # The janitor ran once, in the agent root, labelled as janitor.
    assert len(agent.janitor_invocations) == 1
    jl = agent.janitor_invocations[0]["labels"]
    assert jl["phase"] == "janitor"
    assert jl["task_id"] == blocked.task_id

    # The prerequisite was dispatched in the same run and completed.
    prereq = [t for t in enumerate_tasks(folder) if t.text == "Provision the secret"]
    assert len(prereq) == 1
    state = TaskState.load(folder)
    assert state.get(prereq[0].task_id).status == "complete"

    # The blocked task's line was removed from the plan and its entry
    # reconciled out of tasks.json by the post-janitor resync.
    assert state.get(blocked.task_id) is None
    assert "Call the FOO API" not in (folder / "PLAN.md").read_text()

    # The leftovers folder exists in the agent folder with the moved task, and
    # the janitor's edits were committed by the harness in the agent folder.
    leftovers = agent_root / "01a-api-leftovers"
    assert leftovers.is_dir()
    assert "Call the FOO API" in (leftovers / "PLAN.md").read_text()
    alog = _log_oneline(agent_root, "main")
    assert any(f"janitor {blocked.task_id}" in ln for ln in alog)


# --- Harness metric probe ---


def _read_metrics(folder: Path) -> list[dict]:
    path = folder / ".ola" / "metrics.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_append_metric_sample_writes_json_line(tmp_path):
    append_metric_sample(tmp_path, "cpu", 0.5, "2026-06-24T00:00:00.000Z")
    append_metric_sample(tmp_path, "rss", 12.0, "2026-06-24T00:00:01.000Z")
    rows = _read_metrics(tmp_path)
    assert rows == [
        {"ts": "2026-06-24T00:00:00.000Z", "name": "cpu", "value": 0.5},
        {"ts": "2026-06-24T00:00:01.000Z", "name": "rss", "value": 12.0},
    ]


def test_run_probe_parses_single_object(tmp_path):
    pairs = _run_probe('echo \'{"name": "cpu", "value": 0.5}\'', tmp_path, 5.0)
    assert pairs == [("cpu", 0.5)]


def test_run_probe_parses_array(tmp_path):
    cmd = 'echo \'[{"name": "a", "value": 1}, {"name": "b", "value": 2.5}]\''
    pairs = _run_probe(cmd, tmp_path, 5.0)
    assert pairs == [("a", 1.0), ("b", 2.5)]


def test_run_probe_swallows_nonzero_exit(tmp_path):
    assert _run_probe("exit 1", tmp_path, 5.0) == []


def test_run_probe_swallows_junk_stdout(tmp_path):
    assert _run_probe("echo not-json", tmp_path, 5.0) == []


def test_run_probe_swallows_missing_keys(tmp_path):
    assert _run_probe('echo \'{"name": "cpu"}\'', tmp_path, 5.0) == []
    assert _run_probe('echo \'{"value": "nan"}\'', tmp_path, 5.0) == []


def test_run_probe_swallows_timeout(tmp_path):
    # The probe sleeps far longer than the timeout; the TimeoutExpired is
    # swallowed and no pairs are returned.
    assert _run_probe("sleep 5", tmp_path, 0.2) == []


def test_run_folder_metric_probe_writes_sample(tmp_path):
    """A probe that emits a JSON line writes a sample to metrics.jsonl."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Build the thing\n")
    run_folder(
        _TickingAgent(),
        folder,
        agent_root,
        project,
        initial_cap=1,
        metric_cmd='echo \'{"name": "cpu", "value": 0.5}\'',
    )
    rows = _read_metrics(folder)
    assert rows
    assert rows[0]["name"] == "cpu"
    assert rows[0]["value"] == 0.5
    assert "ts" in rows[0]


def test_run_folder_probe_samples_baseline_then_each_merge_back(tmp_path):
    """The probe runs once as a baseline, then once per tick that merged a
    worktree back — and it runs in the *project* base branch, so it watches the
    merged artefacts climb. With cap=1 the two merges land in distinct ticks, so
    the series is the baseline followed by one bump per merge: [0, 1, 2]."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] one\n- [ ] two\n")
    # An idempotent probe: count merged file_*.txt artefacts in the cwd (the
    # project repo). Re-running it on an unchanged tree yields the same number.
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/bin/sh\n"
        'n=$(ls file_*.txt 2>/dev/null | wc -l | tr -d " ")\n'
        "printf '{\"name\": \"files\", \"value\": %s}\\n' \"$n\"\n"
    )
    probe.chmod(0o755)
    run_folder(
        _TickingAgent(),
        folder,
        agent_root,
        project,
        initial_cap=1,  # serialize so each merge-back lands in its own tick
        metric_cmd=f"sh {probe}",
    )
    values = [r["value"] for r in _read_metrics(folder)]
    assert values == [0.0, 1.0, 2.0]


@pytest.mark.parametrize("cmd", ["exit 1", "echo not-json", "sleep 30"])
def test_run_folder_broken_probe_never_crashes_no_sample(tmp_path, cmd, monkeypatch):
    """A broken probe (non-zero/junk/timeout) leaves the run intact, no sample."""
    # Shrink the probe timeout so the `sleep 30` case trips it quickly rather
    # than blocking the baseline + merge-back probe for the full ceiling.
    monkeypatch.setattr("ola.scheduler.PROBE_TIMEOUT_SEC", 0.2)
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Build the thing\n")
    task = enumerate_tasks(folder)[0]
    run_folder(
        _TickingAgent(),
        folder,
        agent_root,
        project,
        initial_cap=1,
        metric_cmd=cmd,
    )
    # The task still completes — a broken probe never stalls or crashes the run.
    assert task_is_checked(folder, task.task_id) is True
    assert _read_metrics(folder) == []


def test_run_folder_no_metric_cmd_writes_no_file(tmp_path):
    """With no metric_cmd the probe is a no-op and metrics.jsonl is never created."""
    project, agent_root, folder = _two_repos(tmp_path, "- [ ] Build the thing\n")
    run_folder(_TickingAgent(), folder, agent_root, project, initial_cap=1)
    assert not (folder / ".ola" / "metrics.jsonl").exists()
