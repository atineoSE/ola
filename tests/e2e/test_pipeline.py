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

import json

import pytest

from ola.monitor.data import read_folder_status, read_task_rows
from ola.plan import enumerate_tasks
from ola.scheduler import (
    DEFAULT_CONCURRENCY,
    _MAX_STAGNANT_LOOPS,
    FolderIncompleteError,
    write_concurrency,
)

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

    # No .ola/concurrency file → ola materializes the default cap on the first
    # tick, so the file is present and auditable afterward.
    fs = read_folder_status(folder)
    assert fs.is_parallel  # .ola/ exists once the run starts
    assert fs.concurrency_cap == DEFAULT_CONCURRENCY


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


# --- 3. A folder's tasks each emit a task-phase STATS row ---------------------


def test_plan_run_writes_task_stats(tmp_path):
    agent_path = build_example_repo(tmp_path, "01-find-date")
    folder = agent_path / "01-find-date"

    agent = ScriptedAgent()
    run_pipeline(agent, agent_path)

    # Every task completed, and each emitted a ``task-…`` STATS row.
    tasks = enumerate_tasks(folder)
    assert tasks and all(t.checked for t in tasks)
    phases = [r["phase"] for r in read_stats(folder)]
    assert sum(p.startswith("task-") for p in phases) == len(tasks)


# --- 4. Multiple folders processed in order -----------------------------------


def test_multi_folder_ordering(tmp_path):
    # The full example: three plan folders run in index order.
    agent_path = build_example_repo(tmp_path)

    agent = ScriptedAgent()
    run_pipeline(agent, agent_path)

    # All folders fully complete.
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
    # A task that exhausts its attempts and is not relocated leaves PLAN.md
    # unfinished — the harness bails out rather than advancing.
    with pytest.raises(FolderIncompleteError):
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
    # Stagnant-then-terminal leaves the box unticked → bail out.
    with pytest.raises(FolderIncompleteError):
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
    # The backstop halts the spin; the folder is then left unfinished → bail.
    with pytest.raises(FolderIncompleteError):
        run_pipeline(agent, agent_path, max_attempts=1000)

    (task,) = enumerate_tasks(folder)
    assert not task.checked
    # The circuit breaker halts after _MAX_STAGNANT_LOOPS consecutive stalls;
    # dispatch is bounded by that, not by max_attempts.
    assert len(agent.calls) <= _MAX_STAGNANT_LOOPS + 1


# --- 7b. Crash-orphan recovery ------------------------------------------------


def test_crash_orphan_running_is_retried(tmp_path):
    """A task left `running` by a crashed prior run is requeued, not stuck.

    Reproduces the real bug: an interrupted run leaves tasks.json with a
    `running` status whose checkbox is unticked and no worker alive. The next
    run must re-dispatch it (not skip it as already-in-flight) and drive the
    folder to completion without bailing.
    """
    agent_path = build_example_repo(tmp_path, "02-utils")
    folder = agent_path / "02-utils"
    tasks = enumerate_tasks(folder)
    orphan = tasks[0]

    ola_dir = folder / ".ola"
    ola_dir.mkdir(parents=True, exist_ok=True)
    (ola_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "text": t.text,
                        "line_no": t.line_no,
                        "status": "running" if t is orphan else "pending",
                        "attempts": 1 if t is orphan else 0,
                        "last_error": None,
                    }
                    for t in tasks
                ]
            }
        )
    )

    # Completes everything; the orphan is recovered, so no bail-out.
    agent = ScriptedAgent()
    run_pipeline(agent, agent_path)

    assert all(t.checked for t in enumerate_tasks(folder))
    entry = next(e for e in read_tasks(folder) if e["task_id"] == orphan.task_id)
    assert entry["status"] == "complete"
    # The orphan was actually re-dispatched (the agent ran for it).
    assert any(t == orphan.task_id for (_f, t, _a) in agent.calls)


# --- 8. Source edits merge back to the agent branch ---------------------------


def test_source_edit_merges_back(tmp_path):
    agent_path = build_example_repo(tmp_path, "02-utils")
    folder = agent_path / "02-utils"

    # Every task in this scenario writes the *same* file (widget.py), so they
    # are not parallel-safe; pin the cap to 1 to serialize the edits and make
    # the last-writer-wins merge-back deterministic.
    write_concurrency(folder, 1)

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


# --- 10. Blocked task → janitor unblocks --------------------------------------


def test_blocked_task_janitor_unblocks(tmp_path):
    """A blocked task triggers a janitor that injects a prereq and defers the
    task to a leftovers folder that runs before later-numbered folders."""
    agent_path = build_agent_repo(tmp_path, "blocked")
    folder = agent_path / "01-init"

    agent = ScriptedAgent(
        block_tasks={"Call the FOO API": "FOO_API_KEY is not available"}
    )
    run_pipeline(agent, agent_path)

    # The janitor ran exactly once, for the blocked task in 01-init.
    assert len(agent.janitor_calls) == 1
    assert agent.janitor_calls[0][0] == "01-init"

    # The prerequisite was dispatched and completed in the same folder run.
    prereq = [
        t for t in enumerate_tasks(folder) if t.text == "Provision the prerequisite"
    ]
    assert len(prereq) == 1 and prereq[0].checked

    # The blocked task's line is gone from 01-init's plan and its entry was
    # reconciled out of tasks.json by the post-janitor resync.
    assert "Call the FOO API" not in (folder / "PLAN.md").read_text()
    assert all(t["text"] != "Call the FOO API" for t in read_tasks(folder))

    # The leftovers folder exists, ran to completion, and ran BEFORE 02-next.
    leftovers = agent_path / "01a-init-leftovers"
    assert leftovers.is_dir()
    moved = enumerate_tasks(leftovers)
    assert [t.text for t in moved] == ["Call the FOO API"]
    assert moved[0].checked
    call_folders = [f for (f, _t, _a) in agent.calls]
    assert call_folders.index("01a-init-leftovers") < call_folders.index("02-next")

    # The janitor's edits were committed by the harness.
    subjects = git_log_subjects(agent_path)
    assert any(s.startswith("ola: 01-init janitor t-") for s in subjects)

    # Events: a failed event flagged blocked, and a janitor lifecycle that
    # completes — all under the existing envelope.
    events = read_events(folder)
    blocked_failed = [
        e for e in events if e["status"] == "failed" and e["data"].get("blocked")
    ]
    assert len(blocked_failed) == 1
    janitor_events = [e for e in events if e["agent_id"].startswith("janitor-")]
    assert janitor_events
    assert janitor_events[0]["status"] == "started"
    assert janitor_events[0]["data"] == {"role": "janitor"}
    assert janitor_events[-1]["status"] == "complete"


# --- 11. Blocked task → janitor escalates --------------------------------------


def test_blocked_task_janitor_escalates(tmp_path):
    """When unblocking needs a human, the janitor files BLOCKERS.md and the
    pipeline keeps moving past it."""
    agent_path = build_agent_repo(tmp_path, "blocked")
    folder = agent_path / "01-init"

    agent = ScriptedAgent(
        block_tasks={"Call the FOO API": "needs a paid account only a human can open"},
        janitor_action="escalate",
    )
    run_pipeline(agent, agent_path)

    # The blockers folder exists with both explanations, and no PLAN.md, so
    # the outer loop skipped it without stalling.
    blockers = agent_path / "01b-init-blockers"
    assert blockers.is_dir()
    content = (blockers / "BLOCKERS.md").read_text()
    assert "Call the FOO API" in content
    assert "needs a paid account" in content
    assert not (blockers / "PLAN.md").exists()

    # The escalated task's line was removed from the live plan; the rest of
    # the pipeline completed (01-init's other task and all of 02-next).
    assert "Call the FOO API" not in (folder / "PLAN.md").read_text()
    assert all(t.checked for t in enumerate_tasks(folder))
    assert all(t.checked for t in enumerate_tasks(agent_path / "02-next"))


# --- 12. Blocked is terminal: dispatched once despite retries ------------------


def test_blocked_task_never_retried(tmp_path):
    agent_path = build_agent_repo(tmp_path, "blocked")
    folder = agent_path / "01-init"

    agent = ScriptedAgent(
        block_tasks={"Call the FOO API": "missing key"},
    )
    # With the janitor off, the blocked task is never relocated, so its line
    # stays in PLAN.md and the folder is left unfinished → bail out.
    with pytest.raises(FolderIncompleteError):
        run_pipeline(agent, agent_path, max_attempts=3, janitor_enabled=False)

    # With the janitor off, the blocked entry stays in tasks.json, terminal.
    (blocked_entry,) = [
        t for t in read_tasks(folder) if t["text"] == "Call the FOO API"
    ]
    assert blocked_entry["status"] == "blocked"
    assert blocked_entry["attempts"] == 1
    assert blocked_entry["last_error"] == "blocked: missing key"

    # Dispatched exactly once despite max_attempts=3; no janitor ran.
    blocked_calls = [
        (f, t, a) for (f, t, a) in agent.calls if t == blocked_entry["task_id"]
    ]
    assert len(blocked_calls) == 1
    assert agent.janitor_calls == []

    # The monitor surfaces the blocked status.
    fs = read_folder_status(folder)
    assert "blocked" in {r.status for r in fs.task_rows}
