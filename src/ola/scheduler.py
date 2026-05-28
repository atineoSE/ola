"""Per-folder parallel scheduler for the parallel-agents flow.

Replaces the inner ``while True`` loop in :func:`ola.loop._process_folder`.
For each unchecked task in ``<folder>/PLAN.md`` the scheduler:

1. Creates an isolated git worktree at ``<folder>/.ola/worktrees/<task_id>``.
2. Builds a per-task prompt from ``TASK-PROMPT.md`` (folder-local override
   wins; otherwise a built-in default is used) with ``{{task_text}}`` and
   ``{{task_id}}`` substituted.
3. Runs the agent in that worktree.
4. Validates that the agent ticked its checkbox in the worktree's PLAN.md.
   On success, propagates the worktree commit and the PLAN.md tick onto
   the agent-folder branch under a shared lock.

Concurrency is capped at ``initial_cap``. Live re-reading of
``<folder>/.ola/concurrency`` is Phase 5 work. The ``emitter`` slot is a
Phase 6 hookup point; today the scheduler simply ignores ``None``.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ola.agents.base import Agent
from ola.loop import per_task_state_dir
from ola.plan import set_task_checked, task_is_checked
from ola.taskstate import TaskState
from ola.worktree import cleanup, commit, create, merge_back

logger = logging.getLogger(__name__)

_DEFAULT_TASK_PROMPT_FILE = (
    Path(__file__).resolve().parent / "agents" / "TASK-PROMPT.md"
)
_DEFAULT_TASK_PROMPT = _DEFAULT_TASK_PROMPT_FILE.read_text()

# Folder-wide circuit-breaker threshold: halt the folder after this many
# consecutive stagnant attempts (agent reports success but does not tick its
# checkbox). Ported from the old inner loop's ``_MAX_STAGNANT_LOOPS``. The
# stagnation backstop task wires this into the scheduler's main loop.
_MAX_STAGNANT_LOOPS = 5

# Safety cap for rate-limit sleeps in the worker error path. A reset further
# out than this is treated as a failure rather than slept through.
_MAX_RATE_LIMIT_WAIT_SEC = 8 * 3600  # 8 hours


@dataclass
class _Job:
    """Bookkeeping for an in-flight worker future."""

    task_id: str
    attempt: int
    started: float


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True)
    if result.returncode != 0:
        logger.error(
            "git %s (in %s) failed: %s",
            " ".join(args),
            cwd,
            result.stderr.decode(errors="replace"),
        )
        result.check_returncode()
    return result


def _load_task_prompt(folder: Path) -> str:
    """Return the task-prompt template — folder-local file if present, else default."""
    local = folder / "TASK-PROMPT.md"
    if local.exists():
        return local.read_text()
    return _DEFAULT_TASK_PROMPT


def _substitute(template: str, task_text: str, task_id: str) -> str:
    return template.replace("{{task_text}}", task_text).replace("{{task_id}}", task_id)


def _propagate(
    worktree_path: Path,
    folder: Path,
    agent_root: Path,
    task_id: str,
) -> None:
    """Cherry-pick the worktree's HEAD onto *agent_root* and tick PLAN.md.

    Caller must hold the PLAN.md lock. The agent-folder PLAN.md is excluded
    from the cherry-pick and reapplied via :func:`set_task_checked` so two
    concurrent ticks can't conflict on shared lines. The agent's commit
    message is preserved via ``git commit -C <sha>``.
    """
    sha = commit(worktree_path, f"ola: {folder.name} {task_id}")
    plan_rel = f"{folder.name}/PLAN.md"
    merge_back(worktree_path, agent_root, exclude_paths=[plan_rel])
    set_task_checked(folder, task_id, True)
    _git(agent_root, "add", plan_rel)
    _git(agent_root, "commit", "-C", sha)


def _truncate(s: str, n: int = 500) -> str:
    return s if len(s) <= n else s[:n] + "..."


def _run_with_rate_limit_resume(
    agent: Agent,
    prompt: str,
    workdir: str,
    state_dir: str | None,
    labels: dict[str, str],
    on_progress: Any | None,
) -> Any | None:
    """Run the agent, sleeping through rate-limit windows and resuming.

    Moved out of the old inner loop's rate-limit branch: a ``rate_limited``
    response is transient, so the worker sleeps until the reset and re-runs the
    same task rather than burning a retry attempt. Returns the agent response,
    or ``None`` when the reset is further out than ``_MAX_RATE_LIMIT_WAIT_SEC``
    (the caller treats ``None`` as a failure).
    """
    while True:
        response = agent.run(
            prompt,
            workdir,
            state_dir=state_dir,
            labels=labels,
            on_progress=on_progress,
        )
        stats = response.stats
        if not (stats.error_type == "rate_limited" and stats.rate_limit_resets_at):
            return response
        wait_sec = max(0, stats.rate_limit_resets_at - time.time()) + 10
        if wait_sec > _MAX_RATE_LIMIT_WAIT_SEC:
            logger.error(
                "Rate limit reset too far away (%ds) for task %s. Giving up.",
                int(wait_sec),
                labels.get("task_id"),
            )
            return None
        logger.warning(
            "Rate limit hit on task %s. Sleeping %ds, then resuming.",
            labels.get("task_id"),
            int(wait_sec),
        )
        time.sleep(wait_sec)


def _run_one_task(
    agent: Agent,
    folder: Path,
    agent_root: Path,
    task_id: str,
    task_text: str,
    attempt: int,
    template: str,
    plan_lock: threading.Lock,
    state: TaskState,
    state_lock: threading.Lock,
    emitter: Any | None,
) -> None:
    """Run a single task end-to-end in its own worktree.

    On agent success + checkbox tick: propagates to the agent-folder branch
    and cleans up the worktree. On any other outcome: marks the task failed
    and retains the worktree for post-mortem.
    """
    worktree_path = create(folder, task_id)
    try:
        prompt = _substitute(template, task_text, task_id)
        plan_in_worktree = worktree_path / folder.name / "PLAN.md"
        prompt += f"\n\nPLAN.md is located at: {plan_in_worktree}"
        workdir = str(worktree_path)
        state_dir = per_task_state_dir(folder, agent, task_id)
        labels = {
            "folder": folder.name,
            "task_id": task_id,
            "attempt": str(attempt),
        }

        # Phase 6 will wire emitter → on_progress; today emitter is unused.
        on_progress = None

        response = _run_with_rate_limit_resume(
            agent, prompt, workdir, state_dir, labels, on_progress
        )
        if response is None:
            with state_lock:
                state.mark(
                    task_id,
                    "failed",
                    last_error="rate limit reset too far away",
                )
                state.save()
            return

        if not response.success:
            with state_lock:
                state.mark(task_id, "failed", last_error=_truncate(response.output))
                state.save()
            return

        worktree_folder = worktree_path / folder.name
        try:
            ticked = task_is_checked(worktree_folder, task_id)
        except (FileNotFoundError, KeyError):
            ticked = False
        if not ticked:
            # Stagnant: the dedicated stagnation backstop (separate task)
            # will count this toward retries and the folder-wide circuit
            # breaker. For now, surface it as a failure and keep the
            # worktree for debugging.
            with state_lock:
                state.mark(
                    task_id,
                    "failed",
                    last_error="stagnant: agent did not tick its checkbox",
                )
                state.save()
            return

        with plan_lock:
            _propagate(worktree_path, folder, agent_root, task_id)
        with state_lock:
            state.mark(task_id, "complete", last_error=None)
            state.save()
        cleanup(worktree_path, keep_on_failure=False)
    except BaseException as exc:
        with state_lock:
            try:
                state.mark(task_id, "failed", last_error=str(exc))
                state.save()
            except Exception:
                logger.exception("Failed to record task failure for %s", task_id)
        raise


def run_folder(
    agent: Agent,
    folder: Path,
    agent_root: Path,
    initial_cap: int,
    emitter: Any | None = None,
) -> None:
    """Process every pending task in *folder*/PLAN.md in parallel.

    *initial_cap* bounds the number of in-flight workers. Phase 5 will swap
    the static cap for a live re-read of ``<folder>/.ola/concurrency``.
    *emitter* defaults to ``None`` (no events); Phase 6 will populate it.
    """
    folder = Path(folder)
    agent_root = Path(agent_root)

    state = TaskState.sync_from_plan(folder)
    state.save()

    plan_lock = threading.Lock()
    state_lock = threading.Lock()
    template = _load_task_prompt(folder)

    cap = max(initial_cap, 1)
    in_flight: dict[Future, _Job] = {}

    with ThreadPoolExecutor(max_workers=cap) as executor:
        while True:
            while len(in_flight) < cap:
                with state_lock:
                    next_task = state.next_pending()
                    if next_task is None:
                        break
                    new_attempt = next_task.attempts + 1
                    state.mark(next_task.task_id, "running", attempts=new_attempt)
                    state.save()
                    task_id = next_task.task_id
                    task_text = next_task.text
                fut = executor.submit(
                    _run_one_task,
                    agent,
                    folder,
                    agent_root,
                    task_id,
                    task_text,
                    new_attempt,
                    template,
                    plan_lock,
                    state,
                    state_lock,
                    emitter,
                )
                in_flight[fut] = _Job(
                    task_id=task_id,
                    attempt=new_attempt,
                    started=time.monotonic(),
                )

            if not in_flight:
                break

            done, _ = wait(
                list(in_flight.keys()),
                timeout=1.0,
                return_when=FIRST_COMPLETED,
            )
            for fut in done:
                job = in_flight.pop(fut)
                exc = fut.exception()
                if exc is not None:
                    logger.error(
                        "task %s (attempt %d) raised: %s",
                        job.task_id,
                        job.attempt,
                        exc,
                        exc_info=exc,
                    )
