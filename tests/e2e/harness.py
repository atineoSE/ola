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

Happy-path scenarios are sourced straight from the shipped example
(``examples/dummy-project/agent``) via :func:`build_example_repo`, so the
example users run is the same content the suite verifies and cannot silently
rot. Failure-mode scenarios (retry, stagnation) live under
``tests/e2e/fixtures/<name>/`` — their behaviour is scripted in the stub, not
expressible in a runnable example — and are copied via
:func:`build_agent_repo`. Both builders produce an isolated git-backed agent
repo in a tmp dir.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.plan import set_task_checked
from ola.stats import IterationStats

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_AGENT = (
    Path(__file__).resolve().parents[2] / "examples" / "dummy-project" / "agent"
)


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

    ``block_tasks`` maps task text → reason: a matching task run executes the
    *provisioned* ``.ola/bin/ola-blocked`` script (exercising the escape hatch
    end-to-end) and returns without ticking. Each entry blocks only once —
    when the task reappears later (e.g. in a leftovers folder) it falls
    through to ``action``.

    Janitor runs (``labels["phase"] == "janitor"``) behave per
    ``janitor_action``:

    * ``"unblock"`` — appends ``janitor_prereq`` as a new checkbox to the
      current PLAN.md, removes the blocked task's line, and creates the
      leftovers folder dictated by the prompt with the moved task.
    * ``"escalate"`` — creates the blockers folder dictated by the prompt
      with a BLOCKERS.md and removes the blocked task's line.
    """

    mnemonic = "cc"
    state_dir_name = ""  # no per-task state directory needed for the stub

    def __init__(
        self,
        *,
        action: str = "tick",
        source_file: str | None = None,
        fail_until_attempt: int = 0,
        block_tasks: dict[str, str] | None = None,
        janitor_action: str = "unblock",
        janitor_prereq: str = "Provision the prerequisite",
    ) -> None:
        super().__init__()
        self.action = action
        self.source_file = source_file
        self.fail_until_attempt = fail_until_attempt
        self.block_tasks = dict(block_tasks or {})
        self.janitor_action = janitor_action
        self.janitor_prereq = janitor_prereq
        # Recorded (folder, task_id, attempt) for every task run, in order.
        self.calls: list[tuple[str, str, int]] = []
        # Recorded (folder, task_id) for every janitor run, in order.
        self.janitor_calls: list[tuple[str, str]] = []

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        labels = labels or {}
        task_id = labels.get("task_id")

        if labels.get("phase") == "janitor":
            return self._run_janitor(prompt, Path(workdir), labels)

        folder = labels.get("folder", "")
        attempt = int(labels.get("attempt", "1"))
        self.calls.append((folder, task_id, attempt))
        if on_progress:
            on_progress(f"working on {task_id} (attempt {attempt})")

        wt_folder = Path(workdir) / folder

        task_text = self._task_text(prompt)
        if task_text in self.block_tasks:
            reason = self.block_tasks.pop(task_text)  # block only once
            script = Path(workdir) / ".ola" / "bin" / "ola-blocked"
            subprocess.run(
                [str(script), "--reason", reason], capture_output=True, check=True
            )
            return AgentResponse(output="blocked", success=True, stats=IterationStats())

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

    def _run_janitor(self, prompt: str, root: Path, labels: dict) -> AgentResponse:
        """Perform the unblock/escalate edits a real janitor would."""
        folder = root / labels["folder"]
        self.janitor_calls.append((labels["folder"], labels["task_id"]))
        # The blocked task's text and the dictated sibling names, parsed from
        # the substituted JANITOR-PROMPT.
        blocked_text = re.search(r"- Task: (.*?) \(task id", prompt).group(1)
        plan = folder / "PLAN.md"
        lines = [ln for ln in plan.read_text().splitlines() if blocked_text not in ln]

        if self.janitor_action == "unblock":
            sibling_name = re.search(
                r"named exactly `([^`]+-leftovers)`", prompt
            ).group(1)
            lines.append(f"- [ ] {self.janitor_prereq}")
            sibling = root / sibling_name
            sibling.mkdir()
            (sibling / "PLAN.md").write_text(
                f"This task was blocked ({labels['task_id']}); prerequisites are"
                " assumed complete by the time this folder runs.\n\n"
                f"- [ ] {blocked_text}\n"
            )
        else:
            sibling_name = re.search(r"named exactly `([^`]+-blockers)`", prompt).group(
                1
            )
            reason = re.search(r"reason for blocking: (.*)", prompt).group(1)
            sibling = root / sibling_name
            sibling.mkdir()
            (sibling / "BLOCKERS.md").write_text(
                f"# Blocked: {blocked_text}\n\n"
                f"Worker's reason: {reason}\n\n"
                "Janitor: cannot unblock without a human (scripted escalate).\n"
            )

        plan.write_text("\n".join(lines) + "\n")
        return AgentResponse(
            output="janitor done", success=True, stats=IterationStats()
        )

    @staticmethod
    def _task_text(prompt: str) -> str | None:
        m = re.search(r"The task is: (.*?) \(task id", prompt)
        return m.group(1) if m else None

    def version(self) -> str:
        return "e2e-1.0"


# --- Repo / run helpers -------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def _init_agent_repo(agent: Path) -> Path:
    """Turn *agent* into a git repo with the runtime ignore rules.

    ``.ola/`` is gitignored so per-run runtime artifacts (worktrees,
    tasks.json, events.jsonl) never pollute commits, while any
    ``.ola/concurrency`` shipped by the scenario stays on disk for the
    scheduler to read.
    """
    (agent / ".gitignore").write_text(".env\n.ola/\n")
    _git(agent, "init", "-b", "main")
    _git(agent, "config", "user.email", "e2e@example.com")
    _git(agent, "config", "user.name", "E2E")
    _git(agent, "config", "commit.gpgsign", "false")
    _git(agent, "add", "-A")
    _git(agent, "commit", "-m", "fixture import")
    return agent


def build_agent_repo(tmp_path: Path, fixture: str) -> Path:
    """Copy fixture *fixture* into an isolated git-backed agent repo."""
    agent = tmp_path / "agent"
    shutil.copytree(FIXTURES / fixture, agent)
    return _init_agent_repo(agent)


def build_example_repo(tmp_path: Path, *folders: str) -> Path:
    """Copy phase folders from ``examples/dummy-project/agent`` into a repo.

    With no *folders* given, copies every ``NN-…`` phase folder of the
    example. Only the phase folders are copied (not ``.env.example`` or the
    example's own ``.gitignore``), keeping the run hermetic.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    names = folders or sorted(
        p.name for p in EXAMPLE_AGENT.iterdir() if p.is_dir() and p.name[0].isdigit()
    )
    for name in names:
        shutil.copytree(EXAMPLE_AGENT / name, agent / name)
    return _init_agent_repo(agent)


def run_pipeline(
    agent: Agent,
    agent_path: Path,
    *,
    max_attempts: int = 0,
    janitor_enabled: bool = True,
) -> None:
    """Run the full outer loop over *agent_path* with *agent*."""
    from ola.loop import run_outer_loop

    run_outer_loop(
        agent, agent_path, max_attempts=max_attempts, janitor_enabled=janitor_enabled
    )


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
