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

A SIGINT/SIGTERM mid-run is caught (see :class:`RunInterrupted`): the
scheduler flushes a terminal snapshot for every in-flight task — tasks.json
plus a ``failed``/``interrupted`` event — before stopping, so a killed run
is observable rather than a silent freeze at ``running``. Catchable signals
aside, the main loop also refreshes a ``.ola/heartbeat.json`` liveness file
every tick (see :func:`write_heartbeat`), so even an *uncatchable* kill
(SIGKILL/OOM) leaves a durable last-alive timestamp and in-flight snapshot.
"""

from __future__ import annotations

import json
import logging
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ola.agents.base import Agent
from ola.blocked import (
    clear_blocked_record,
    provision_blocked_script,
    read_blocked_record,
    write_blocked_record,
)
from ola.blocked import BlockedRecord
from ola.events.schema import metrics_block
from ola.janitor import run_janitor
from ola.loop import _append_stats, _exclude_ola_artifacts, per_task_state_dir
from ola.plan import count_tasks, set_task_checked, task_is_checked
from ola.taskstate import TaskState
from ola.worktree import (
    MergeBackConflict,
    cleanup,
    commit,
    create,
    merge_back,
    prune_branch,
)

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

# Default in-flight worker bound when ``.ola/concurrency`` is missing/malformed.
# ``run_folder`` also materializes the file at this value on the first tick, so
# the cap is always present on disk and auditable from the start of a run — and
# the monitors always have a number to show rather than a placeholder.
DEFAULT_CONCURRENCY = 2

# How often the scheduler refreshes its liveness heartbeat (see
# ``write_heartbeat``). The main loop ticks at ~1s; this throttles the file
# write to keep disk churn low while staying fine-grained enough that a stall is
# obvious (a heartbeat older than a small multiple of this means the loop is no
# longer ticking).
HEARTBEAT_INTERVAL_SEC = 5.0

# Default cadence (seconds) for the optional harness metric probe. When a
# ``metric_cmd`` is configured the main loop runs it no more than once per this
# interval — its own monotonic gate, independent of the heartbeat — and appends
# the parsed samples to ``.ola/metrics.jsonl``. With no ``metric_cmd`` the probe
# never runs and the file is never created (the fallback to current behaviour).
DEFAULT_METRIC_INTERVAL = 15.0

# Worker outcomes reported back to the main loop, which folds them into the
# folder-wide stagnation counter. ``STAGNANT`` (agent reported success but did
# not tick its checkbox) advances the counter; anything else resets it.
# ``BLOCKED`` (task self-reported as blocked via the ola-blocked script) is
# terminal for the task — never retried — and triggers a janitor run.
_OUTCOME_COMPLETE = "complete"
_OUTCOME_FAILED = "failed"
_OUTCOME_STAGNANT = "stagnant"
_OUTCOME_BLOCKED = "blocked"


class FolderIncompleteError(RuntimeError):
    """A folder drained with PLAN.md checkboxes still unticked.

    The harness drives every task in a folder to completion (a ticked
    checkbox) or relocation (the janitor moves a blocked task into a sibling
    ``…-leftovers``/``…-blockers`` folder, removing its line). If, after every
    task has either ticked, been relocated, or exhausted ``--max-attempts``,
    unticked lines remain, the folder is stuck: it can neither complete nor
    advance without silently abandoning work. The harness bails out rather
    than move on. ``remaining`` is the count of still-unticked tasks.
    """

    def __init__(self, folder_name: str, remaining: int) -> None:
        self.folder_name = folder_name
        self.remaining = remaining
        super().__init__(
            f"{folder_name}: {remaining} task(s) could not be completed or "
            f"relocated to leftovers/blockers. Stopping."
        )


class RunInterrupted(RuntimeError):
    """The scheduler was stopped by a SIGINT/SIGTERM partway through a folder.

    Raised only *after* the in-flight snapshot has been flushed — every
    ``running`` task recorded as ``failed`` in tasks.json with an
    ``interrupted: …`` reason and a terminal ``failed`` event (carrying
    ``data.interrupted = true``) emitted for it — so the interruption is
    observable rather than a silent freeze. This is distinct from
    :class:`FolderIncompleteError`, which means the folder genuinely could not
    be driven to completion: an interrupt is the operator stopping the run, not
    a stuck plan. It propagates through the outer loop to the CLI, which logs a
    clean message and exits; the next ``ola`` invocation re-derives every task
    from PLAN.md, so the ``failed`` snapshot never gates the re-run.
    """

    def __init__(self, folder_name: str, signum: int | None) -> None:
        self.folder_name = folder_name
        self.signum = signum
        super().__init__(f"{folder_name}: run interrupted by {_signame(signum)}.")


def _signame(signum: int | None) -> str:
    """Human-readable name for a signal number (``SIGTERM``), tolerant of junk."""
    if not signum:
        return "a signal"
    try:
        return signal.Signals(signum).name
    except ValueError:
        return str(signum)


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


def read_concurrency(folder: Path, default: int = DEFAULT_CONCURRENCY) -> int:
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


def write_concurrency(folder: Path, value: int) -> None:
    """Atomically set the live concurrency cap in ``<folder>/.ola/concurrency``.

    The mirror of :func:`read_concurrency` and the dashboard's only write: the
    parallel-agents slider calls this to retarget a live run. The scheduler
    re-reads the file every tick, so the new cap takes effect on the next tick.
    ``0`` is a valid value ("pause new starts; let in-flight workers finish").
    Negative values are rejected, matching ``read_concurrency``'s contract.
    """
    if value < 0:
        raise ValueError(f"concurrency cap must be >= 0, got {value}")
    cap_file = folder / ".ola" / "concurrency"
    cap_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cap_file.with_name(cap_file.name + ".tmp")
    tmp.write_text(f"{value}\n")
    tmp.replace(cap_file)


def _utc_now_iso() -> str:
    """UTC timestamp as ``YYYY-MM-DDThh:mm:ss.sssZ`` (matches the event ``ts``)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def write_heartbeat(folder: Path, payload: dict[str, Any]) -> None:
    """Atomically write the scheduler liveness heartbeat to ``.ola/heartbeat.json``.

    Distinct from the ``events.jsonl`` stream: an event is task-attempt-scoped
    and reaches disk through a queued writer thread, so an in-flight event can
    be lost if the process is hard-killed. The heartbeat is *scheduler*-scoped
    and written synchronously from the main loop (tmp + rename), so the last
    value is always on disk — it survives even an uncatchable SIGKILL (an OOM
    kill being the motivating case). A consumer detects a stall by comparing
    ``now`` against the file's ``ts`` while tasks are still ``running``; the
    ``in_flight`` snapshot then names exactly which tasks were live when the
    scheduler went silent — the post-mortem signal a frozen ``tasks.json``
    (everything stuck at ``running``) cannot give.
    """
    path = folder / ".ola" / "heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n")
    tmp.replace(path)


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


def append_metric_sample(folder: Path, name: str, value: float, ts: str) -> None:
    """Append one metric sample as a JSON line to ``<folder>/.ola/metrics.jsonl``.

    Mirrors the append idiom of :func:`ola.loop._append_stats`: one
    ``{"ts", "name", "value"}`` object per line, opened in append mode so
    concurrent samples never clobber earlier ones. *ts* is the
    :func:`_utc_now_iso` timestamp of the probe that produced the sample.
    """
    path = folder / ".ola" / "metrics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"ts": ts, "name": name, "value": value}) + "\n")


def _run_probe(
    cmd: str, cwd: Path, timeout: float
) -> list[tuple[str, float]]:
    """Run a metric probe and parse its stdout JSON into ``(name, value)`` pairs.

    The probe is an arbitrary shell command emitting JSON on stdout: either a
    single ``{"name", "value"}`` object or an array of them. Every failure mode
    is swallowed — a timeout, a missing/unrunnable command, a non-zero exit, or
    malformed/unexpected JSON — and yields an empty list, so a broken probe
    never crashes, stalls, or error-logs the run (modelled on the version-probe
    in :mod:`ola.agents.codex`: no ``check_returncode()``, no error log like
    :func:`_git`).
    """
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    pairs: list[tuple[str, float]] = []
    try:
        for item in items:
            pairs.append((item["name"], float(item["value"])))
    except (KeyError, TypeError, ValueError):
        return []
    return pairs


def _load_task_prompt(folder: Path) -> str:
    """Return the task-prompt template — folder-local file if present, else default."""
    local = folder / "TASK-PROMPT.md"
    if local.exists():
        return local.read_text()
    return _DEFAULT_TASK_PROMPT


def _substitute(
    template: str,
    task_text: str,
    task_id: str,
    blocked_cmd: str = "",
    plan_path: str = "",
) -> str:
    return (
        template.replace("{{task_text}}", task_text)
        .replace("{{task_id}}", task_id)
        .replace("{{blocked_cmd}}", blocked_cmd)
        .replace("{{plan_path}}", plan_path)
    )


def _propagate(
    worktree_path: Path,
    folder: Path,
    agent_root: Path,
    project_path: Path,
    task_id: str,
    base_sha: str,
) -> None:
    """Land the worktree's code on *project_path* and tick PLAN.md in *agent_root*.

    Caller must hold the PLAN.md lock. The agent's worktree commit (project
    code only — the plan copy under ``.ola/`` is git-excluded) is cherry-picked
    onto the project repo and committed there with the agent's original message
    (``git commit -C <sha>``). The checkbox is then ticked in the agent folder's
    PLAN.md via :func:`set_task_checked` and committed separately in the agent
    folder, so concurrent ticks never conflict on shared plan lines.

    *base_sha* is the worktree's HEAD at creation. When the agent ticked its
    checkbox without changing any project code, the worktree commit equals
    *base_sha* (nothing new to cherry-pick), so the project repo is left
    untouched and only the tick is committed in the agent folder.
    """
    sha = commit(worktree_path, f"ola: {folder.name} {task_id}")
    if sha != base_sha:
        merge_back(worktree_path, project_path)
        # The merge can net to nothing new on the project repo when a sibling
        # task already landed identical content (e.g. a shared empty
        # __init__.py): an identical add is no diff against HEAD. Only commit
        # when the index actually advances, so `commit -C` never fails on an
        # empty index — the identical-add-only task is a clean no-op here.
        staged = (
            _git(project_path, "diff", "--cached", "--name-only")
            .stdout.decode()
            .strip()
        )
        if staged:
            _git(project_path, "commit", "-C", sha)
    set_task_checked(folder, task_id, True)
    plan_rel = f"{folder.name}/PLAN.md"
    _git(agent_root, "add", plan_rel)
    _git(agent_root, "commit", "-m", f"ola: {folder.name} {task_id}")


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


def _handle_merge_conflict(
    conflict: MergeBackConflict,
    folder: Path,
    task_id: str,
    attempt: int,
    max_attempts: int,
    state: TaskState,
    state_lock: threading.Lock,
    prog: _ProgressEmitter,
    stats: Any | None,
) -> str:
    """Resolve a merge-back collision: retry within the run, else escalate.

    A :class:`~ola.worktree.MergeBackConflict` is a *reconciliation* collision,
    not a hard failure — a sibling task changed overlapping lines after this
    worktree branched. It is never stagnation, so the returned outcome resets
    the folder's stagnation breaker.

    While attempts remain it is a non-stagnant failed attempt: the task is
    requeued (``_OUTCOME_FAILED``), and because :func:`worktree.create` re-anchors
    the next worktree on the *current* project HEAD, the retry sees the winner's
    already-landed files and usually merges cleanly — most collisions (e.g. the
    shared empty ``__init__.py``) self-heal this way with no new state, intra-run
    only. A task that still conflicts after its whole ``--max-attempts`` budget
    is genuine coupling (a plan-independence violation); it is recorded as
    blocked (``_OUTCOME_BLOCKED``) so the existing janitor escalation relocates
    it to a blockers folder for a human, rather than smart-merging in the hot
    path.
    """
    paths = ", ".join(conflict.conflicted_paths)
    if attempt < max_attempts:
        logger.warning(
            "task %s (attempt %d) merge-back conflicted on %s; requeuing.",
            task_id,
            attempt,
            paths,
        )
        _fail_or_requeue(
            state,
            state_lock,
            task_id,
            attempt,
            max_attempts,
            f"merge-back conflict: {paths}",
        )
        prog.failed(f"merge-back conflict: {paths}", stats=stats)
        return _OUTCOME_FAILED

    reason = (
        f"merge-back conflict persisted after {attempt} attempt(s) on {paths} "
        f"— likely real coupling between tasks (a plan-independence violation)."
    )
    logger.warning("task %s exhausted retries on merge-back conflict: %s", task_id, reason)
    write_blocked_record(folder, task_id, reason)
    with state_lock:
        state.mark(task_id, "blocked", last_error=f"blocked: {reason}")
        state.save()
    prog.blocked(reason, stats=stats)
    return _OUTCOME_BLOCKED


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
    project_path: Path,
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
    """Run a single task end-to-end in its own project-repo worktree.

    The worktree is branched from *project_path* and the live folder PLAN.md is
    copied into ``<worktree>/.ola/PLAN.md`` for the agent to read and tick (the
    project repo has no numbered folder of its own). On agent success + checkbox
    tick: the code lands on the project repo and the tick is committed to the
    agent folder, then the worktree is cleaned up. On any other outcome: marks
    the task failed (or requeues it for retry) and retains the worktree.

    Returns one of the ``_OUTCOME_*`` constants so the main loop can fold the
    result into the folder-wide stagnation counter. A raised exception is an
    out-of-band hard failure that the main loop treats as non-stagnant.
    """
    worktree_path = create(project_path, folder, task_id)
    # The worktree's HEAD at creation: if the agent ticks without changing any
    # project code, its commit equals this and there is nothing to merge back.
    base_sha = _git(worktree_path, "rev-parse", "HEAD").stdout.decode().strip()
    # The project worktree has no numbered folder; stage the live PLAN.md into
    # the worktree's .ola/ so the agent reads and ticks it there. .ola/ is
    # git-excluded, so the tick never rides the cherry-pick back to the project.
    plan_copy = worktree_path / ".ola" / "PLAN.md"
    plan_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(folder / "PLAN.md", plan_copy)
    plan_dir = plan_copy.parent
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
        prompt = _substitute(
            template, task_text, task_id, str(blocked_cmd), str(plan_copy)
        )
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

        tasks_before = count_tasks(plan_dir)
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
        tasks_after = count_tasks(plan_dir)
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
            ticked = task_is_checked(plan_dir, task_id)
        except (FileNotFoundError, KeyError):
            ticked = False
        record = read_blocked_record(folder, task_id)

        if ticked and response.success:
            # Checkbox is truth: a tick wins even over a stray blocked marker.
            clear_blocked_record(folder, task_id)
            try:
                with plan_lock:
                    _propagate(
                        worktree_path,
                        folder,
                        agent_root,
                        project_path,
                        task_id,
                        base_sha,
                    )
            except MergeBackConflict as conflict:
                return _handle_merge_conflict(
                    conflict,
                    folder,
                    task_id,
                    attempt,
                    max_attempts,
                    state,
                    state_lock,
                    prog,
                    response.stats,
                )
            with state_lock:
                state.mark(task_id, "complete", last_error=None)
                state.save()
            cleanup(worktree_path, keep_on_failure=False)
            # Work is merged and the worktree is gone; drop the now-vestigial
            # task branch so a completed run leaves no dangling ola/* refs.
            prune_branch(project_path, folder, task_id)
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
    project_path: Path,
    initial_cap: int,
    emitter: Any | None = None,
    max_attempts: int = 0,
    janitor_enabled: bool = True,
    metric_cmd: str | None = None,
    metric_interval: float = DEFAULT_METRIC_INTERVAL,
) -> None:
    """Process every pending task in *folder*/PLAN.md in parallel.

    *agent_root* is the agent folder (holds *folder* and receives checkbox
    ticks); *project_path* is the project repo the per-task worktrees branch
    from and where the agent's code lands.

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
    *metric_cmd* is an optional shell command run periodically (throttled to
    *metric_interval* seconds, its own monotonic gate beside the heartbeat) that
    emits JSON metric samples appended to ``.ola/metrics.jsonl`` (see
    :func:`_run_probe`/:func:`append_metric_sample`). When ``None`` the probe is
    a no-op and no file is written — the fallback to current behaviour.
    """
    folder = Path(folder)
    agent_root = Path(agent_root)
    project_path = Path(project_path)

    # Keep .ola/ runtime artifacts out of `git add -A` commits even when the
    # caller didn't go through run_outer_loop's _ensure_git. On the project repo
    # this covers the per-task worktrees, the staged PLAN.md copy, and the
    # provisioned ola-blocked script; on the agent folder it covers the janitor's
    # `git add -A` commits over events/tasks/blocked state.
    _exclude_ola_artifacts(project_path)
    _exclude_ola_artifacts(agent_root)

    # One-time, thread-unsafe agent setup runs here, on the main thread, before
    # any worker is dispatched, so a backend that initialises process-global
    # state can do it once, serially. All current backends shell out to a CLI
    # subprocess (no in-process model imports), so this is a no-op for them —
    # but the hook stays for backend-agnosticism. Default is a no-op.
    agent.warm_up()

    state = TaskState.sync_from_plan(folder)
    state.save()

    plan_lock = threading.Lock()
    state_lock = threading.Lock()
    stats_lock = threading.Lock()
    template = _load_task_prompt(folder)

    # Default cap when the concurrency file is missing/malformed. The live
    # value (including 0 to pause) is re-read on every tick below.
    live_default = max(initial_cap, 1)

    # Materialize the cap on disk when absent so it is auditable from the first
    # tick and the monitors always have a value to show (never a "-"). Only when
    # missing — never clobber a value the user or the dashboard slider has set.
    if not (folder / ".ola" / "concurrency").exists():
        write_concurrency(folder, live_default)

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

    # Graceful, observable shutdown on SIGINT/SIGTERM. A scheduler killed
    # mid-run otherwise leaves every in-flight worker frozen at ``running``
    # with its last event a mid-stream ``working`` — the silent-death signature
    # that makes a stall undiagnosable. We instead catch the signal, flush a
    # terminal snapshot the instant it arrives, and raise RunInterrupted.
    interrupted = threading.Event()
    interrupt_signum: dict[str, int] = {}

    def _on_signal(signum: int, _frame: Any) -> None:
        # Minimal and async-signal-safe: record the first signal and set the
        # flag. The real flush runs on the main loop's next tick (≤1s), where
        # taking locks and touching disk is safe.
        interrupt_signum.setdefault("signum", signum)
        interrupted.set()

    prev_handlers: dict[int, Any] = {}
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            prev_handlers[_sig] = signal.signal(_sig, _on_signal)
        except ValueError:
            # signal.signal only works in the main thread of the main
            # interpreter; when run_folder is driven from a worker thread (some
            # tests, embedders) we skip custom handling and behave as before.
            pass

    def _flush_interrupt() -> None:
        """Record every in-flight task as interrupted, synchronously.

        Runs the moment a signal is observed, *before* the executor drains, so
        the on-disk snapshot (tasks.json) and the event stream both record the
        interruption even if the operator follows up with a SIGKILL. A worker
        that genuinely completes after this still wins — it re-marks its own
        task under ``state_lock`` — so this only ever rescues the truly
        abandoned ones from a frozen ``running``.
        """
        signame = _signame(interrupt_signum.get("signum"))
        reason = f"interrupted: scheduler received {signame}"
        with state_lock:
            for job in in_flight.values():
                entry = state.get(job.task_id)
                if entry is not None and entry.status == "running":
                    state.mark(job.task_id, "failed", last_error=reason)
                if emitter is not None:
                    emitter.failed(
                        agent_id=f"agent-{job.task_id}",
                        attempt=job.attempt,
                        folder=folder.name,
                        task_id=job.task_id,
                        task_text=entry.text if entry else job.task_id,
                        agent_backend=agent.mnemonic,
                        data={"error": reason, "interrupted": True},
                    )
            state.save()
        logger.warning(
            "Scheduler received %s — recorded %d in-flight task(s) as "
            "interrupted in %s and stopping.",
            signame,
            len(in_flight),
            folder.name,
        )

    # Liveness heartbeat: a small sidecar the main loop refreshes every tick
    # (throttled to HEARTBEAT_INTERVAL_SEC) so a stalled or hard-killed run is
    # detectable from disk — the gap a frozen tasks.json leaves open.
    current_cap = live_default
    last_heartbeat = 0.0

    def _write_heartbeat(*, force: bool = False) -> None:
        nonlocal last_heartbeat
        now_m = time.monotonic()
        if not force and now_m - last_heartbeat < HEARTBEAT_INTERVAL_SEC:
            return
        last_heartbeat = now_m
        with state_lock:
            pending = sum(1 for e in state.all() if e.status == "pending")
        snapshot = [
            {
                "task_id": job.task_id,
                "attempt": job.attempt,
                "elapsed_s": round(now_m - job.started, 1),
            }
            for job in in_flight.values()
        ]
        write_heartbeat(
            folder,
            {
                "ts": _utc_now_iso(),
                "folder": folder.name,
                "cap": current_cap,
                "running": len(snapshot),
                "pending": pending,
                "in_flight": snapshot,
            },
        )

    # Optional harness metric probe: same shape as the heartbeat (own monotonic
    # gate, throttled by metric_interval). A None metric_cmd makes this a no-op
    # so no metrics.jsonl is written — the fallback to current behaviour.
    last_metric = 0.0

    def _sample_probe() -> None:
        nonlocal last_metric
        if metric_cmd is None:
            return
        now_m = time.monotonic()
        if now_m - last_metric < metric_interval:
            return
        last_metric = now_m
        pairs = _run_probe(metric_cmd, folder, timeout=min(metric_interval, 10.0))
        ts = _utc_now_iso()
        for name, value in pairs:
            append_metric_sample(folder, name, value, ts)

    try:
        while True:
            if interrupted.is_set():
                _flush_interrupt()
                raise RunInterrupted(folder.name, interrupt_signum.get("signum"))
            cap = read_concurrency(folder, default=live_default)
            current_cap = cap
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
                    project_path,
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

            # Beat after dispatch so the snapshot reflects the just-started
            # workers; throttled internally. Runs on every path below — the
            # active wait and the cap-0 paused sleep — so a paused-but-alive
            # scheduler still beats.
            _write_heartbeat()
            _sample_probe()

            wait_set: list[Future] = list(in_flight.keys())
            if janitor_future is not None:
                wait_set.append(janitor_future)

            if not wait_set and not janitor_queue:
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
        # Final beat records the terminal state (running 0 on a clean drain;
        # the still-parked workers on an interrupt). A consumer reads "loop
        # stopped here" rather than a stale mid-run snapshot.
        _write_heartbeat(force=True)
        for _sig, _prev in prev_handlers.items():
            try:
                signal.signal(_sig, _prev)
            except (ValueError, TypeError):
                # Best-effort restore: the prior handler may be a C-level one
                # getsignal reports as None, which signal.signal refuses.
                pass
        # On a clean drain we wait for in-flight workers. On interrupt we don't
        # stack a blocking join on top of the signal — the snapshot is already
        # flushed. (The interpreter's concurrent.futures atexit hook still joins
        # live worker threads, so a hung in-process worker — e.g. the oh backend
        # mid-call — can keep the process alive until a SIGKILL; by then
        # tasks.json already records the interruption.)
        drain = not interrupted.is_set()
        executor.shutdown(wait=drain)
        for retired in retired_executors:
            retired.shutdown(wait=drain)
        janitor_pool.shutdown(wait=drain)
