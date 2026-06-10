"""End-to-end tests for the ola pipeline against a scripted stub agent.

Each test copies an agent folder into an isolated git repo, runs the real
outer loop, and asserts on the resulting artifacts across every dimension the
pipeline produces: PLAN.md checkboxes, the agent-repo git history,
``.ola/tasks.json``, ``STATS.jsonl``, ``.ola/events.jsonl``, worktree
lifecycle, source-edit merge-back, and the ola-top monitor data layer.

Happy-path scenarios run directly off ``examples/dummy-project/agent`` — the
shipped example doubles as the test corpus, so these tests also guarantee the
example keeps parsing and scheduling correctly. Failure-mode scenarios
(retry, stagnation) use test-only fixtures under ``tests/e2e/fixtures/``.
"""

from __future__ import annotations

from ola.monitor.data import read_folder_status, read_task_rows
from ola.plan import enumerate_tasks
from ola.scheduler import _MAX_STAGNANT_LOOPS

from .harness import (
    ScriptedAgent,
    build_agent_repo,
    build_example_repo,
    git_log_subjects,
    read_events,
    read_stats,
    read_tasks,
    run_pipeline,
    worktree_dir,
)


def _statuses(events: list[dict], task_id: str) -> list[str]:
    return [e["status"] for e in events if e["task_id"] == task_id]


# --- 1. Plain sequential plan -------------------------------------------------


def test_sequential_plan_all_complete(tmp_path):
    agent_path = build_example_repo(tmp_path, "02-utils")
    folder = agent_path / "02-utils"

    run_pipeline(ScriptedAgent(), agent_path)

    # Every checkbox ticked.
    tasks = enumerate_tasks(folder)
    assert len(tasks) == 3
    assert all(t.checked for t in tasks)

    # tasks.json spine: all complete.
    assert {t["status"] for t in read_tasks(folder)} == {"complete"}

    # One per-task commit per task landed on the agent branch.
    subjects = git_log_subjects(agent_path)
    assert sum(s.startswith("ola: 02-utils ") for s in subjects) == 3

    # No .ola/concurrency file → sequential (cap 1).
    fs = read_folder_status(folder)
    assert fs.is_parallel  # .ola/ exists once the run starts
    assert fs.concurrency_cap == 1


# --- 2. Parallel plan ---------------------------------------------------------


def test_parallel_plan_all_land_on_branch(tmp_path):
    agent_path = build_example_repo(tmp_path, "03-parallel")
    folder = agent_path / "03-parallel"

    agent = ScriptedAgent()
    run_pipeline(agent, agent_path)

    tasks = enumerate_tasks(folder)
    assert len(tasks) == 4
    assert all(t.checked for t in tasks)
    assert {t["status"] for t in read_tasks(folder)} == {"complete"}

    # Four distinct per-task commits.
    subjects = git_log_subjects(agent_path)
    assert sum(s.startswith("ola: 03-parallel ") for s in subjects) == 4

    # Worktrees cleaned up on success.
    for t in tasks:
        assert not worktree_dir(folder, t.task_id).exists()

    # Monitor sees a parallel folder at the example's shipped cap of 2 with
    # all tasks complete.
    fs = read_folder_status(folder)
    assert fs.is_parallel
    assert fs.concurrency_cap == 2
    assert fs.tasks_completed == 4
    assert {r.status for r in fs.task_rows} == {"complete"}


# --- 3. Seed phase generates the plan, then it executes -----------------------


def test_seed_then_execute(tmp_path):
    agent_path = build_example_repo(tmp_path, "01-find-date")
    folder = agent_path / "01-find-date"
    assert not (folder / "PLAN.md").exists()

    agent = ScriptedAgent(seed_plan="- [ ] Seeded task A\n- [ ] Seeded task B\n")
    run_pipeline(agent, agent_path)

    # Seed wrote PLAN.md and both tasks completed.
    tasks = enumerate_tasks(folder)
    assert len(tasks) == 2
    assert all(t.checked for t in tasks)

    # A seed commit and a seed STATS row exist.
    subjects = git_log_subjects(agent_path)
    assert any(s == "ola: 01-find-date seed" for s in subjects)
    phases = [r["phase"] for r in read_stats(folder)]
    assert "seed" in phases
    assert sum(p.startswith("task-") for p in phases) == 2


# --- 4. Multiple folders processed in order -----------------------------------


def test_multi_folder_ordering(tmp_path):
    # The full example: a seed folder, then a sequential and a parallel plan.
    agent_path = build_example_repo(tmp_path)

    agent = ScriptedAgent()
    run_pipeline(agent, agent_path)

    # All folders fully complete (01-find-date's plan comes from the seed).
    names = ("01-find-date", "02-utils", "03-parallel")
    for name in names:
        tasks = enumerate_tasks(agent_path / name)
        assert tasks and all(t.checked for t in tasks), name

    # Folders run in index order: every call for folder N precedes any call
    # for folder N+1.
    folders_in_call_order = [folder for (folder, _tid, _att) in agent.calls]
    for earlier, later in zip(names, names[1:]):
        earlier_idxs = [i for i, f in enumerate(folders_in_call_order) if f == earlier]
        later_idxs = [i for i, f in enumerate(folders_in_call_order) if f == later]
        assert max(earlier_idxs) < min(later_idxs), (earlier, later)


# --- 5. Failure then retry ----------------------------------------------------


def test_failure_then_retry_succeeds(tmp_path):
    agent_path = build_agent_repo(tmp_path, "failure-retry")
    folder = agent_path / "01-retry"

    # Fail attempt 1, succeed on attempt 2; allow up to 2 attempts.
    agent = ScriptedAgent(fail_until_attempt=2)
    run_pipeline(agent, agent_path, max_attempts=2)

    (task,) = enumerate_tasks(folder)
    assert task.checked
    (entry,) = read_tasks(folder)
    assert entry["status"] == "complete"
    assert entry["attempts"] == 2

    # The agent was dispatched exactly twice for the one task.
    assert [att for (_f, _t, att) in agent.calls] == [1, 2]

    # Events show a failed first attempt and a complete second attempt.
    statuses = _statuses(read_events(folder), task.task_id)
    assert "failed" in statuses
    assert "complete" in statuses


# --- 6. Terminal failure (no retries) -----------------------------------------


def test_terminal_failure_keeps_worktree(tmp_path):
    agent_path = build_agent_repo(tmp_path, "failure-retry")
    folder = agent_path / "01-retry"

    agent = ScriptedAgent(action="fail")
    run_pipeline(agent, agent_path, max_attempts=0)

    (task,) = enumerate_tasks(folder)
    assert not task.checked
    (entry,) = read_tasks(folder)
    assert entry["status"] == "failed"

    # Worktree retained for post-mortem on terminal failure.
    assert worktree_dir(folder, task.task_id).exists()

    fs = read_folder_status(folder)
    assert {r.status for r in fs.task_rows} == {"failed"}


# --- 7. Stagnation: agent claims success but never ticks ----------------------


def test_stagnant_task_marked_failed(tmp_path):
    agent_path = build_agent_repo(tmp_path, "stagnation")
    folder = agent_path / "01-stuck"

    agent = ScriptedAgent(action="stagnant")
    run_pipeline(agent, agent_path, max_attempts=0)

    (task,) = enumerate_tasks(folder)
    assert not task.checked
    (entry,) = read_tasks(folder)
    assert entry["status"] == "failed"
    assert "stagnant" in (entry.get("last_error") or "")


def test_stagnation_backstop_bounds_retries(tmp_path):
    """A perpetually-stagnant task can't spin forever even with retries to burn."""
    agent_path = build_agent_repo(tmp_path, "stagnation")
    folder = agent_path / "01-stuck"

    # Plenty of retries available; the folder-wide backstop must halt first.
    agent = ScriptedAgent(action="stagnant")
    run_pipeline(agent, agent_path, max_attempts=1000)

    (task,) = enumerate_tasks(folder)
    assert not task.checked
    # The circuit breaker halts after _MAX_STAGNANT_LOOPS consecutive stalls;
    # dispatch is bounded by that, not by max_attempts.
    assert len(agent.calls) <= _MAX_STAGNANT_LOOPS + 1


# --- 8. Source edits merge back to the agent branch ---------------------------


def test_source_edit_merges_back(tmp_path):
    agent_path = build_example_repo(tmp_path, "02-utils")
    folder = agent_path / "02-utils"

    agent = ScriptedAgent(source_file="widget.py")
    run_pipeline(agent, agent_path)

    # The file each task wrote in its worktree was cherry-picked onto the
    # agent branch (PLAN.md tick excluded from the cherry-pick, applied
    # separately) and is present + committed. Tasks run sequentially, so the
    # last task's edit is the surviving content.
    merged = folder / "widget.py"
    assert merged.exists()
    assert "implemented by" in merged.read_text()

    tracked = git_log_subjects(agent_path)
    assert any(s.startswith("ola: 02-utils ") for s in tracked)

    tasks = enumerate_tasks(folder)
    assert tasks and all(t.checked for t in tasks)


# --- 9. Events lifecycle envelope sequence ------------------------------------


def test_events_lifecycle_per_task(tmp_path):
    agent_path = build_example_repo(tmp_path, "03-parallel")
    folder = agent_path / "03-parallel"

    run_pipeline(ScriptedAgent(), agent_path)

    events = read_events(folder)
    assert events, "events.jsonl should be written for a parallel run"
    # Every event carries a known lifecycle status.
    assert {e["status"] for e in events} <= {"started", "working", "complete", "failed"}

    for task in enumerate_tasks(folder):
        statuses = _statuses(events, task.task_id)
        assert statuses[0] == "started"
        assert statuses[-1] == "complete"

    # Monitor's per-task reader folds events in without error.
    rows = read_task_rows(folder)
    assert {r.task_id for r in rows} == {t.task_id for t in enumerate_tasks(folder)}
