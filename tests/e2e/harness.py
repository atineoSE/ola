"""Shared harness for ola end-to-end tests.

These tests drive the *real* outer-loop pipeline
(``ola.loop.run_outer_loop`` → ``ola.scheduler.run_folder`` → worktree
create/commit/merge-back → PLAN.md tick → ``tasks.json``/``STATS.jsonl``/
``events.jsonl``) against a scripted, in-process stub agent. No network, no
real coding agent, fully deterministic — suitable for CI.

The stub agent (:class:`ScriptedAgent`) performs the file mutations a real
agent would (ticking checkboxes, optionally editing source files, or
deliberately failing/stalling), so the surrounding harness logic is exercised
exactly as in production.

Fixtures live under ``tests/e2e/fixtures/<name>/`` as ready-to-copy agent
folders (one or more ``NN-…`` plan subfolders). :func:`build_agent_repo`
copies one into an isolated git-backed agent repo in a tmp dir.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.plan import set_task_checked
from ola.stats import IterationStats

FIXTURES = Path(__file__).parent / "fixtures"


# --- Stub agent ---------------------------------------------------------------


class ScriptedAgent(Agent):
    """An in-process agent whose per-task behaviour is scripted.

    ``action`` selects the outcome of every *task* run:

    * ``"tick"``      — tick the task's checkbox and report success (the
      happy path; also writes ``source_file`` first when given).
    * ``"stagnant"``  — report success but never tick (the scheduler treats
      this as a stagnant attempt).
    * ``"fail"``      — report failure.

    ``fail_until_attempt`` overrides ``action`` for early attempts: any attempt
    whose number is below it fails, later attempts fall through to ``action``.
    Use it with ``--max-attempts`` to exercise retries.

    Seed phase (a run with no ``task_id`` label) writes ``seed_plan`` to the
    PLAN.md path named in the prompt.
    """

    mnemonic = "cc"
    state_dir_name = ""  # no per-task state directory needed for the stub

    def __init__(
        self,
        *,
        action: str = "tick",
        source_file: str | None = None,
        fail_until_attempt: int = 0,
        seed_plan: str = "- [ ] Seeded task\n",
    ) -> None:
        super().__init__()
        self.action = action
        self.source_file = source_file
        self.fail_until_attempt = fail_until_attempt
        self.seed_plan = seed_plan
        # Recorded (folder, task_id, attempt) for every task run, in order.
        self.calls: list[tuple[str, str, int]] = []

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        labels = labels or {}
        task_id = labels.get("task_id")

        # Seed phase: no task_id. Write the configured plan to the named path.
        if task_id is None:
            self._write_seed_plan(prompt)
            return AgentResponse(output="seeded", success=True, stats=IterationStats())

        folder = labels.get("folder", "")
        attempt = int(labels.get("attempt", "1"))
        self.calls.append((folder, task_id, attempt))
        if on_progress:
            on_progress(f"working on {task_id} (attempt {attempt})")

        wt_folder = Path(workdir) / folder

        fail_now = self.fail_until_attempt and attempt < self.fail_until_attempt
        if fail_now or self.action == "fail":
            return AgentResponse(output="failed", success=False, stats=IterationStats())
        if self.action == "stagnant":
            # Claim success without ticking — the harness must detect this.
            return AgentResponse(output="no tick", success=True, stats=IterationStats())

        # Happy path: optionally edit a source file, then tick the checkbox.
        if self.source_file:
            target = wt_folder / self.source_file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"implemented by {task_id}\n")
        set_task_checked(wt_folder, task_id, True)
        return AgentResponse(
            output="done",
            success=True,
            stats=IterationStats(input_tokens=10, output_tokens=5),
        )

    def _write_seed_plan(self, prompt: str) -> None:
        for token in prompt.split():
            if token.endswith("PLAN.md") and "/" in token:
                Path(token).write_text(self.seed_plan)
                return

    def version(self) -> str:
        return "e2e-1.0"


# --- Repo / run helpers -------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def build_agent_repo(tmp_path: Path, fixture: str) -> Path:
    """Copy fixture *fixture* into an isolated git-backed agent repo.

    Returns the agent-folder path. ``.ola/`` is gitignored so per-run runtime
    artifacts (worktrees, tasks.json, events.jsonl) never pollute commits,
    while any ``.ola/concurrency`` shipped by the fixture stays on disk for the
    scheduler to read.
    """
    agent = tmp_path / "agent"
    shutil.copytree(FIXTURES / fixture, agent)
    (agent / ".gitignore").write_text(".env\n.ola/\n")
    _git(agent, "init", "-b", "main")
    _git(agent, "config", "user.email", "e2e@example.com")
    _git(agent, "config", "user.name", "E2E")
    _git(agent, "config", "commit.gpgsign", "false")
    _git(agent, "add", "-A")
    _git(agent, "commit", "-m", "fixture import")
    return agent


def run_pipeline(agent: Agent, agent_path: Path, *, max_attempts: int = 0) -> None:
    """Run the full outer loop over *agent_path* with *agent*."""
    from ola.loop import run_outer_loop

    run_outer_loop(agent, agent_path, max_attempts=max_attempts)


# --- Inspection helpers -------------------------------------------------------


def read_tasks(folder: Path) -> list[dict]:
    """Return the task entries from ``<folder>/.ola/tasks.json``."""
    data = json.loads((folder / ".ola" / "tasks.json").read_text())
    return data["tasks"]


def read_events(folder: Path) -> list[dict]:
    """Return all events from ``<folder>/.ola/events.jsonl`` in file order."""
    path = folder / ".ola" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_stats(folder: Path) -> list[dict]:
    """Return all rows from ``<folder>/STATS.jsonl`` in file order."""
    path = folder / "STATS.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def git_log_subjects(repo: Path) -> list[str]:
    """Return commit subjects (newest first) for *repo*."""
    out = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [ln for ln in out.splitlines() if ln.strip()]


def worktree_dir(folder: Path, task_id: str) -> Path:
    """Path to a task's worktree under the folder's ``.ola/``."""
    return folder / ".ola" / "worktrees" / task_id
