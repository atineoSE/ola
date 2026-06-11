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

Concurrency is bounded by a live cap re-read from
``<folder>/.ola/concurrency`` on every scheduler tick: raising the file's
value spawns new workers up to the new cap on the next tick, lowering it
(including to ``0`` to pause) leaves running workers untouched and only
gates new starts. When an ``Emitter`` is supplied each worker emits an
event stream (``started`` → ``working*`` → ``complete``/``failed``); an
``emitter`` of ``None`` disables events entirely.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ola.agents.base import Agent
from ola.blocked import (
    clear_blocked_record,
    provision_blocked_script,
    read_blocked_record,
)
from ola.blocked import BlockedRecord
from ola.events.schema import metrics_block
from ola.janitor import run_janitor
from ola.loop import _append_stats, _exclude_ola_artifacts, per_task_state_dir
from ola.plan import count_tasks, set_task_checked, task_is_checked
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

# Worker outcomes reported back to the main loop, which folds them into the
# folder-wide stagnation counter. ``STAGNANT`` (agent reported success but did
# not tick its checkbox) advances the counter; anything else resets it.
# ``BLOCKED`` (task self-reported as blocked via the ola-blocked script) is
# terminal for the task — never retried — and triggers a janitor run.
_OUTCOME_COMPLETE = "complete"
_OUTCOME_FAILED = "failed"
_OUTCOME_STAGNANT = "stagnant"
_OUTCOME_BLOCKED = "blocked"


@dataclass
class _Job:
    """Bookkeeping for an in-flight worker future."""

    task_id: str
    attempt: int
    started: float


class _ProgressEmitter:
    """Per-worker facade over an optional :class:`~ola.events.client.Emitter`.

    Holds the envelope fields common to one task attempt (``agent_id``,
    ``attempt``, ``folder``, ``task_id``, ``task_text``, ``agent_backend``) so
    the worker only supplies the status-specific bits, and coalesces ``working``
    events to at most one per second per worker (the rate the agent's
    ``on_progress`` callback fires at can be much higher).

    When the wrapped emitter is ``None`` every method is a no-op, so the
    scheduler behaves identically with events disabled — the pre-Phase-6
    default. ``agent_backend`` is the agent's mnemonic read as a *value* for the
    envelope, never branched on, so the events path stays agent-agnostic.
    """

    def __init__(
        self,
        emitter: Any | None,
        *,
        agent_id: str,
        attempt: int,
        folder: str,
        task_id: str,
        task_text: str,
        agent_backend: str,
    ) -> None:
        self._emitter = emitter
        self._common: dict[str, Any] = {
            "agent_id": agent_id,
            "attempt": attempt,
            "folder": folder,
            "task_id": task_id,
            "task_text": task_text,
            "agent_backend": agent_backend,
        }
        self._last_working = 0.0
        self._working_lock = threading.Lock()

    def started(self) -> None:
        if self._emitter is not None:
            self._emitter.started(**self._common)

    def working(self, message: str, metrics: dict[str, Any] | None = None) -> None:
        """Emit a ``working`` event, dropping it if one fired < 1s ago."""
        if self._emitter is None:
            return
        now = time.monotonic()
        with self._working_lock:
            if now - self._last_working < 1.0:
                return
            self._last_working = now
        data: dict[str, Any] = {"message": message}
        if metrics:
            data["metrics"] = metrics
        self._emitter.working(**self._common, data=data)

    def complete(self, stats: Any | None = None) -> None:
        if self._emitter is not None:
            metrics = _final_metrics(stats)
            data = {"metrics": metrics} if metrics else None
            self._emitter.complete(**self._common, data=data)

    def failed(self, error: str | None = None, stats: Any | None = None) -> None:
        if self._emitter is not None:
            data: dict[str, Any] = {}
            if error:
                data["error"] = error
            metrics = _final_metrics(stats)
            if metrics:
                data["metrics"] = metrics
            self._emitter.failed(**self._common, data=data or None)

    def blocked(self, reason: str, stats: Any | None = None) -> None:
        """Emit a ``failed`` event carrying the additive ``blocked`` flag.

        The event schema has no ``blocked`` status; per SCHEMA.md consumers
        ignore unknown ``data`` keys, so a self-reported blockage rides as a
        ``failed`` event with ``data.blocked = true``.
        """
        if self._emitter is None:
            return
        data: dict[str, Any] = {"error": f"blocked: {reason}", "blocked": True}
        metrics = _final_metrics(stats)
        if metrics:
            data["metrics"] = metrics
        self._emitter.failed(**self._common, data=data)


def _final_metrics(stats: Any | None) -> dict[str, Any] | None:
    """Build the terminal ``metrics`` block from an attempt's IterationStats.

    Returns ``None`` when the backend reported no throughput numbers at all —
    the block is optional in the schema, so absence beats a row of zeros.
    """
    if stats is None:
        return None
    output_tokens = getattr(stats, "output_tokens", 0)
    decode_ms = getattr(stats, "decode_ms", 0)
    if not output_tokens and not decode_ms:
        return None
    return metrics_block(output_tokens=output_tokens, decode_ms=decode_ms)


def read_concurrency(folder: Path, default: int = 1) -> int:
    """Read the live concurrency cap from ``<folder>/.ola/concurrency``.

    Re-read by the scheduler on every tick so the cap can be adjusted at
    runtime. Parsing rules:

    - **Missing file** → *default* (silent; the absence of the file is the
      normal "no parallelism configured" case).
    - **Malformed** (not a single integer) → *default*, logging a warning.
    - **Negative** → rejected, returning *default* and logging a warning.
    - **Zero** → returned as-is. A cap of ``0`` means "pause new starts; let
      in-flight workers finish" — it is a valid, distinct state, not malformed.
    - **Positive** → returned as-is.
    """
    cap_file = folder / ".ola" / "concurrency"
    try:
        raw = cap_file.read_text()
    except FileNotFoundError:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning(
            "Malformed concurrency cap in %s (%r); using default %d.",
            cap_file,
            raw.strip(),
            default,
        )
        return default
    if value < 0:
        logger.warning(
            "Negative concurrency cap in %s (%d); using default %d.",
            cap_file,
            value,
            default,
        )
        return default
    return value


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


def _substitute(
    template: str, task_text: str, task_id: str, blocked_cmd: str = ""
) -> str:
    return (
        template.replace("{{task_text}}", task_text)
        .replace("{{task_id}}", task_id)
        .replace("{{blocked_cmd}}", blocked_cmd)
    )


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


def _fail_or_requeue(
    state: TaskState,
    state_lock: threading.Lock,
    task_id: str,
    attempt: int,
    max_attempts: int,
    last_error: str | None,
) -> None:
    """Record a failed attempt: requeue it for retry, or fail it terminally.

    When this attempt is below the ``max_attempts`` ceiling the task is set
    back to ``pending`` so the main loop re-dispatches the same ``task_id``
    with ``attempt += 1``. Otherwise the task stays ``failed`` and PLAN.md is
    left unchanged for that line. A requeued task's stale worktree is cleared
    by :func:`worktree.create` on the next dispatch.
    """
    requeue = attempt < max_attempts
    with state_lock:
        state.mark(task_id, "pending" if requeue else "failed", last_error=last_error)
        state.save()
    if requeue:
        logger.info(
            "task %s failed on attempt %d; requeuing (max_attempts=%d).",
            task_id,
            attempt,
            max_attempts,
        )


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
    max_attempts: int,
    template: str,
    plan_lock: threading.Lock,
    state: TaskState,
    state_lock: threading.Lock,
    stats_lock: threading.Lock,
    emitter: Any | None,
) -> str:
    """Run a single task end-to-end in its own worktree.

    On agent success + checkbox tick: propagates to the agent-folder branch
    and cleans up the worktree. On any other outcome: marks the task failed
    (or requeues it for retry) and retains the worktree for post-mortem.

    Returns one of the ``_OUTCOME_*`` constants so the main loop can fold the
    result into the folder-wide stagnation counter. A raised exception is an
    out-of-band hard failure that the main loop treats as non-stagnant.
    """
    worktree_path = create(folder, task_id)
    worktree_folder = worktree_path / folder.name
    clear_blocked_record(folder, task_id)
    blocked_cmd = provision_blocked_script(worktree_path, folder, task_id)
    prog = _ProgressEmitter(
        emitter,
        agent_id=f"agent-{task_id}",
        attempt=attempt,
        folder=folder.name,
        task_id=task_id,
        task_text=task_text,
        agent_backend=agent.mnemonic,
    )
    try:
        prog.started()
        prompt = _substitute(template, task_text, task_id, str(blocked_cmd))
        plan_in_worktree = worktree_folder / "PLAN.md"
        prompt += f"\n\nPLAN.md is located at: {plan_in_worktree}"
        workdir = str(worktree_path)
        state_dir = per_task_state_dir(folder, agent, task_id)
        labels = {
            "folder": folder.name,
            "task_id": task_id,
            "attempt": str(attempt),
        }

        # The agent's coarse progress feeds `working` events (coalesced to at
        # most one per second by _ProgressEmitter); a None emitter no-ops.
        on_progress = prog.working

        tasks_before = count_tasks(worktree_folder)
        t0 = time.monotonic()
        response = _run_with_rate_limit_resume(
            agent, prompt, workdir, state_dir, labels, on_progress
        )
        wall_ms = int((time.monotonic() - t0) * 1000)
        if response is None:
            with state_lock:
                state.mark(
                    task_id,
                    "failed",
                    last_error="rate limit reset too far away",
                )
                state.save()
            prog.failed("rate limit reset too far away")
            return _OUTCOME_FAILED

        # Record a STATS.jsonl row for this attempt. The phase is the
        # parallel-mode shape ``task-<task_id>-<attempt>``; the monitor parser
        # treats phase as an opaque string.
        tasks_after = count_tasks(worktree_folder)
        with stats_lock:
            _append_stats(
                folder,
                f"task-{task_id}-{attempt}",
                response.stats,
                wall_ms,
                agent,
                tasks_before,
                tasks_after,
            )

        try:
            ticked = task_is_checked(worktree_folder, task_id)
        except (FileNotFoundError, KeyError):
            ticked = False
        record = read_blocked_record(folder, task_id)

        if ticked and response.success:
            # Checkbox is truth: a tick wins even over a stray blocked marker.
            clear_blocked_record(folder, task_id)
            with plan_lock:
                _propagate(worktree_path, folder, agent_root, task_id)
            with state_lock:
                state.mark(task_id, "complete", last_error=None)
                state.save()
            cleanup(worktree_path, keep_on_failure=False)
            prog.complete(stats=response.stats)
            return _OUTCOME_COMPLETE

        if record is not None:
            # The task self-reported as blocked via the ola-blocked script.
            # Terminal for this task — no retry regardless of --max-attempts;
            # the main loop dispatches a janitor to arrange unblocking. The
            # reason is recorded, so the worktree holds nothing worth keeping.
            logger.warning(
                "task %s (attempt %d) reported BLOCKED: %s",
                task_id,
                attempt,
                record.reason,
            )
            with state_lock:
                state.mark(task_id, "blocked", last_error=f"blocked: {record.reason}")
                state.save()
            cleanup(worktree_path, keep_on_failure=False)
            prog.blocked(record.reason, stats=response.stats)
            return _OUTCOME_BLOCKED

        if not response.success:
            _fail_or_requeue(
                state,
                state_lock,
                task_id,
                attempt,
                max_attempts,
                _truncate(response.output),
            )
            prog.failed(_truncate(response.output), stats=response.stats)
            return _OUTCOME_FAILED

        # Stagnant: the agent reported success but never ticked its
        # checkbox, so the work it claimed isn't done. Skip merge_back,
        # count the attempt toward --max-attempts (requeue if any remain),
        # and keep the worktree for debugging. The returned outcome
        # advances the folder-wide stagnation circuit breaker.
        logger.warning(
            "task %s (attempt %d) reported success but did not tick its "
            "checkbox — stagnant.",
            task_id,
            attempt,
        )
        _fail_or_requeue(
            state,
            state_lock,
            task_id,
            attempt,
            max_attempts,
            "stagnant: agent did not tick its checkbox",
        )
        prog.failed("stagnant: agent did not tick its checkbox", stats=response.stats)
        return _OUTCOME_STAGNANT
    except BaseException as exc:
        with state_lock:
            try:
                state.mark(task_id, "failed", last_error=str(exc))
                state.save()
            except Exception:
                logger.exception("Failed to record task failure for %s", task_id)
        prog.failed(str(exc))
        raise


def run_folder(
    agent: Agent,
    folder: Path,
    agent_root: Path,
    initial_cap: int,
    emitter: Any | None = None,
    max_attempts: int = 0,
    janitor_enabled: bool = True,
) -> None:
    """Process every pending task in *folder*/PLAN.md in parallel.

    *initial_cap* seeds the in-flight worker bound, but the live cap is
    re-read from ``<folder>/.ola/concurrency`` on every tick: an increase
    spawns new workers up to the new cap on the next tick, a decrease does
    not preempt running workers, and a cap of ``0`` pauses new starts while
    letting in-flight workers finish. *initial_cap* also supplies the default
    when the concurrency file is missing or malformed.
    *emitter* is an optional :class:`~ola.events.client.Emitter`; when supplied
    each worker emits ``started``/``working``/``complete``/``failed`` events for
    its attempt. It defaults to ``None`` (events disabled).
    *max_attempts* is the retry ceiling: a worker that fails is requeued (same
    ``task_id``, ``attempt += 1``) while its attempt count is below this value;
    the default ``0`` means no retries.
    *janitor_enabled* controls whether a task that self-reports BLOCKED
    dispatches a janitor run (see :mod:`ola.janitor`); when ``False`` the task
    simply stays ``blocked`` in tasks.json.
    """
    folder = Path(folder)
    agent_root = Path(agent_root)

    # Keep .ola/ runtime artifacts (provisioned ola-blocked scripts, sidecar
    # state) out of worktree `git add -A` commits even when the caller didn't
    # go through run_outer_loop's _ensure_git.
    _exclude_ola_artifacts(agent_root)

    state = TaskState.sync_from_plan(folder)
    state.save()

    plan_lock = threading.Lock()
    state_lock = threading.Lock()
    stats_lock = threading.Lock()
    template = _load_task_prompt(folder)

    # Default cap when the concurrency file is missing/malformed. The live
    # value (including 0 to pause) is re-read on every tick below.
    live_default = max(initial_cap, 1)
    in_flight: dict[Future, _Job] = {}

    # The pool's max_workers can't shrink, so we size it to the largest cap
    # observed so far and grow it by spawning a fresh, larger executor when a
    # live increase exceeds the current ceiling. Workers already in flight on a
    # retired executor keep running until they finish; the retired executors are
    # drained in the ``finally`` block. Decreases never touch the pool — they
    # only gate new starts via the ``len(in_flight) < cap`` check.
    executor_cap = live_default
    executor = ThreadPoolExecutor(max_workers=executor_cap)
    retired_executors: list[ThreadPoolExecutor] = []

    # Folder-wide circuit breaker: count consecutive stagnant attempts across
    # any tasks; reset on any non-stagnant outcome (real success or a
    # non-stagnant failure). When it reaches _MAX_STAGNANT_LOOPS we halt the
    # folder rather than spin forever on an agent that never makes progress.
    consecutive_stagnant = 0
    halted = False

    # Janitor lane: at most one janitor runs per folder, off to the side of
    # the worker pool — it is harness overhead, not a plan task, so it never
    # occupies a concurrency slot. Further blockers queue behind it; a later
    # janitor sees the earlier one's PLAN.md edits via fresh reads.
    janitor_queue: deque[tuple[BlockedRecord, str, int]] = deque()
    janitor_future: Future | None = None
    janitor_pool = ThreadPoolExecutor(max_workers=1)

    def _janitor_job(record: BlockedRecord, task_text: str, attempt: int) -> bool:
        ok = run_janitor(
            agent,
            folder,
            agent_root,
            record,
            task_text,
            attempt,
            plan_lock,
            emitter,
        )
        # Reconcile the live plan in place: janitor-added prerequisite
        # checkboxes become pending entries dispatchable in this same run,
        # and the removed blocked line is dropped from tasks.json.
        with state_lock:
            state.resync()
            state.save()
        return ok

    try:
        while True:
            cap = read_concurrency(folder, default=live_default)
            if cap > executor_cap:
                # Live increase past the current ceiling: retire the old pool
                # (its in-flight workers finish on it) and grow.
                retired_executors.append(executor)
                executor_cap = cap
                executor = ThreadPoolExecutor(max_workers=executor_cap)

            while not halted and len(in_flight) < cap:
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
                    max_attempts,
                    template,
                    plan_lock,
                    state,
                    state_lock,
                    stats_lock,
                    emitter,
                )
                in_flight[fut] = _Job(
                    task_id=task_id,
                    attempt=new_attempt,
                    started=time.monotonic(),
                )

            # Dispatch the next queued janitor when the lane is free.
            if janitor_future is None and janitor_queue:
                record, blocked_text, blocked_attempt = janitor_queue.popleft()
                janitor_future = janitor_pool.submit(
                    _janitor_job, record, blocked_text, blocked_attempt
                )

            wait_set: list[Future] = list(in_flight.keys())
            if janitor_future is not None:
                wait_set.append(janitor_future)

            if not wait_set:
                if halted:
                    break
                with state_lock:
                    pending_remains = state.next_pending() is not None
                if not pending_remains:
                    break
                # Nothing running but tasks remain pending: the cap is 0
                # (paused). Wait a tick and re-read the cap rather than exit —
                # raising the cap later resumes dispatch.
                time.sleep(1.0)
                continue

            done, _ = wait(
                wait_set,
                timeout=1.0,
                return_when=FIRST_COMPLETED,
            )
            for fut in done:
                if fut is janitor_future:
                    # A janitor crash must not kill the folder; the blocked
                    # task simply stays blocked (it is recorded in tasks.json
                    # and .ola/blocked/ for post-mortem).
                    exc = fut.exception()
                    if exc is not None:
                        logger.error("janitor run raised: %s", exc, exc_info=exc)
                    janitor_future = None
                    continue
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
                    # A hard error is a (non-stagnant) failure: reset the run.
                    consecutive_stagnant = 0
                    continue
                outcome = fut.result()
                if outcome == _OUTCOME_STAGNANT:
                    consecutive_stagnant += 1
                else:
                    # BLOCKED is signal, not stagnation — like any other
                    # non-stagnant outcome it resets the circuit breaker.
                    consecutive_stagnant = 0
                if outcome == _OUTCOME_BLOCKED and janitor_enabled:
                    record = read_blocked_record(folder, job.task_id)
                    if record is None:
                        logger.error(
                            "blocked marker for task %s vanished before the "
                            "janitor could be dispatched.",
                            job.task_id,
                        )
                    else:
                        with state_lock:
                            entry = state.get(job.task_id)
                            blocked_text = entry.text if entry else job.task_id
                        janitor_queue.append((record, blocked_text, job.attempt))

            if consecutive_stagnant >= _MAX_STAGNANT_LOOPS:
                logger.warning(
                    "No task progress for %d attempts in %s"
                    " — breaking to avoid infinite loop. tasks=%s",
                    _MAX_STAGNANT_LOOPS,
                    folder.name,
                    count_tasks(folder),
                )
                halted = True
                break
    finally:
        executor.shutdown(wait=True)
        for retired in retired_executors:
            retired.shutdown(wait=True)
        janitor_pool.shutdown(wait=True)
