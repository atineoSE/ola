"""Data layer for the ola-top monitor: parse agent folders into status models."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ola.plan import enumerate_tasks, parse_task_counts
from ola.stats import cache_hit_rate as _cache_hit_rate
from ola.taskstate import TaskState


_AGENT_FULL_NAMES: dict[str, str] = {
    "cc": "Claude Code",
    "ct": "Claude Code (TUI)",
    "oh": "OpenHands",
    "cx": "Codex",
}


@dataclass
class IterationStatus:
    """Stats for a single iteration (a ``task-<id>-<attempt>`` row)."""

    phase: str
    wall_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    agent: str = ""
    agent_version: str = ""
    models: list[str] = field(default_factory=list)
    tool_ms: int = 0
    llm_ms: int = 0
    ttft_ms: int = 0
    streamed: bool = True
    tasks_completed: int = 0
    tasks_total: int = 0
    tasks_completed_delta: int = 0
    max_input_tokens: int = 0
    error_type: str | None = None
    error_message: str | None = None
    rate_limit_resets_at: int | None = None

    @property
    def agent_display(self) -> str:
        """Full agent name with version, e.g. 'Claude Code 1.2.3'."""
        name = _AGENT_FULL_NAMES.get(self.agent, self.agent)
        if self.agent_version:
            return f"{name} {self.agent_version}"
        return name

    @property
    def cache_hit_rate(self) -> float:
        """Cache hit rate as a percentage (0-100)."""
        return _cache_hit_rate(self.input_tokens, self.cache_read_tokens)

    @property
    def avg_input_tokens(self) -> int:
        """Average input tokens per LLM call."""
        if self.num_turns == 0:
            return 0
        return self.input_tokens // self.num_turns

    @property
    def io_ratio(self) -> float:
        """Input/output token ratio."""
        if self.output_tokens == 0:
            return 0.0
        return self.input_tokens / self.output_tokens

    @property
    def time_breakdown(self) -> tuple[float, float]:
        """(llm_pct, tool_pct) as percentages of wall time."""
        if self.wall_ms == 0:
            return (0.0, 0.0)
        tool_pct = self.tool_ms / self.wall_ms * 100
        llm_pct = 100.0 - tool_pct
        return (llm_pct, tool_pct)

    @property
    def llm_tok_per_sec(self) -> float:
        """Output tokens per second during decode (excluding tool time and TTFT)."""
        decode_ms = self.wall_ms - self.tool_ms - self.ttft_ms
        if decode_ms <= 0:
            return 0.0
        return self.output_tokens / (decode_ms / 1000)


@dataclass
class FolderStatus:
    """Aggregated status for one agent subfolder."""

    name: str
    tasks_completed: int = 0
    tasks_total: int = 0
    iterations: list[IterationStatus] = field(default_factory=list)
    # Parallel-mode (``.ola/``) extras. ``concurrency_cap`` is ``None`` for
    # legacy/sequential folders and an int (the live cap) when ``.ola/`` is
    # present; ``task_rows`` is the per-task spine for the expanded view.
    concurrency_cap: int | None = None
    task_rows: list[TaskRow] = field(default_factory=list)
    # Agent mnemonic ("cc"/"oh"/"cx"/"ct") from the latest event that carries
    # one, so the agent shows from task start — events land a ``started`` event
    # immediately, whereas STATS.jsonl (which also carries the agent *version*)
    # is only written at iteration end. Empty for sequential folders (no events)
    # and before the first event lands. See :attr:`agent_display`.
    event_agent_backend: str = ""
    # Parallel-mode wall-clock span across all events (earliest→latest ts),
    # recomputed each read. 0.0 for sequential folders or before two events
    # land. See :attr:`display_wall_ms`.
    events_elapsed_s: float = 0.0

    @property
    def is_parallel(self) -> bool:
        """True when this folder runs in parallel mode (``.ola/`` present)."""
        return self.concurrency_cap is not None

    @property
    def display_wall_ms(self) -> int:
        """Wall time for the folder row's Time column.

        Parallel folders use the live events span (``events_elapsed_s``), so an
        interrupt/resume can't leave a stale ``STATS.jsonl``-summed number that
        reads shorter than a single task. Sequential folders fall back to the
        summed per-iteration wall time.
        """
        if self.is_parallel and self.events_elapsed_s > 0:
            return int(self.events_elapsed_s * 1000)
        return self.total_wall_ms

    @property
    def running_count(self) -> int:
        """Number of tasks currently in the ``running`` state."""
        return sum(1 for r in self.task_rows if r.status == "running")

    @property
    def total_input_tokens(self) -> int:
        return sum(it.input_tokens for it in self.iterations)

    @property
    def total_output_tokens(self) -> int:
        return sum(it.output_tokens for it in self.iterations)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(it.cache_read_tokens for it in self.iterations)

    @property
    def total_cache_creation_tokens(self) -> int:
        return sum(it.cache_creation_tokens for it in self.iterations)

    @property
    def total_wall_ms(self) -> int:
        return sum(it.wall_ms for it in self.iterations)

    @property
    def cache_hit_rate(self) -> float:
        """Aggregate cache hit rate as a percentage (0-100)."""
        return _cache_hit_rate(self.total_input_tokens, self.total_cache_read_tokens)

    @property
    def agent_display(self) -> str:
        """Agent display, e.g. 'Claude Code 1.2.3'.

        Prefers the most recent STATS.jsonl iteration, which carries the agent
        version. Before the first iteration is written, falls back to the agent
        mnemonic from the latest event (no version available there) so the agent
        shows from task start rather than only once the first iteration ends.
        """
        if self.iterations:
            return self.iterations[-1].agent_display
        if self.event_agent_backend:
            return _AGENT_FULL_NAMES.get(
                self.event_agent_backend, self.event_agent_backend
            )
        return ""

    @property
    def total_num_turns(self) -> int:
        return sum(it.num_turns for it in self.iterations)

    @property
    def avg_input_tokens(self) -> int:
        """Average input tokens per LLM call across all iterations."""
        turns = self.total_num_turns
        if turns == 0:
            return 0
        return self.total_input_tokens // turns

    @property
    def max_input_tokens(self) -> int:
        """Max input tokens across all iterations."""
        if not self.iterations:
            return 0
        return max(it.max_input_tokens for it in self.iterations)

    @property
    def total_tool_ms(self) -> int:
        return sum(it.tool_ms for it in self.iterations)

    @property
    def all_streamed(self) -> bool:
        """True if every iteration used streaming (TTFT data is meaningful)."""
        return all(it.streamed for it in self.iterations) if self.iterations else True

    @property
    def median_ttft_ms(self) -> int:
        vals = [it.ttft_ms for it in self.iterations if it.ttft_ms > 0]
        if not vals:
            return 0
        return round(statistics.median(vals))

    @property
    def io_ratio(self) -> float:
        """Input/output token ratio."""
        if self.total_output_tokens == 0:
            return 0.0
        return self.total_input_tokens / self.total_output_tokens

    @property
    def time_breakdown(self) -> tuple[float, float]:
        """(llm_pct, tool_pct) as percentages of wall time."""
        wall = self.total_wall_ms
        if wall == 0:
            return (0.0, 0.0)
        tool_pct = self.total_tool_ms / wall * 100
        llm_pct = 100.0 - tool_pct
        return (llm_pct, tool_pct)

    @property
    def llm_tok_per_sec(self) -> float:
        """Median tok/sec across iterations."""
        vals = [it.llm_tok_per_sec for it in self.iterations if it.llm_tok_per_sec > 0]
        if not vals:
            return 0.0
        return statistics.median(vals)

    @property
    def model_display(self) -> str:
        """Unique model names across all iterations, comma-separated."""
        seen: list[str] = []
        for it in self.iterations:
            for m in it.models:
                if m and m not in seen:
                    seen.append(m)
        return ", ".join(seen)


def parse_stats_jsonl(stats_text: str) -> list[IterationStatus]:
    """Parse STATS.jsonl text into a list of IterationStatus objects."""
    iterations: list[IterationStatus] = []
    for line in stats_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        iterations.append(
            IterationStatus(
                phase=record["phase"],
                wall_ms=record.get("wall_ms", 0),
                input_tokens=record.get("input_tokens", 0),
                output_tokens=record.get("output_tokens", 0),
                cache_read_tokens=record.get("cache_read_tokens", 0),
                cache_creation_tokens=record.get("cache_creation_tokens", 0),
                num_turns=record.get("num_turns", 0),
                agent=record.get("agent", ""),
                agent_version=record.get("agent_version", ""),
                models=record.get("models", []),
                tool_ms=record.get("tool_ms", 0),
                llm_ms=record.get("llm_ms", 0),
                ttft_ms=record.get("ttft_ms", 0),
                streamed=record.get("streamed", True),
                tasks_completed=record.get("tasks_completed", 0),
                tasks_total=record.get("tasks_total", 0),
                tasks_completed_delta=record.get("tasks_completed_delta", 0),
                max_input_tokens=record.get("max_input_tokens", 0),
                error_type=record.get("error_type"),
                error_message=record.get("error_message"),
                rate_limit_resets_at=record.get("rate_limit_resets_at"),
            )
        )
    return iterations


def read_folder_status(folder: Path) -> FolderStatus:
    """Read a single agent subfolder and return its FolderStatus."""
    status = FolderStatus(name=folder.name)

    plan_file = folder / "PLAN.md"
    if plan_file.exists():
        status.tasks_completed, status.tasks_total = parse_task_counts(
            plan_file.read_text()
        )

    stats_file = folder / "STATS.jsonl"
    if stats_file.exists():
        status.iterations = parse_stats_jsonl(stats_file.read_text())

    # Parallel-mode folders carry a ``.ola/`` sidecar. When present, fold in the
    # live concurrency cap and the per-task spine for the expanded view.
    if (folder / ".ola").is_dir():
        # Imported lazily so the monitor doesn't pull in the scheduler's agent
        # and worktree dependencies just to read a single integer.
        from ola.scheduler import read_concurrency

        status.concurrency_cap = read_concurrency(folder)
        status.task_rows = read_task_rows(folder, status.iterations)
        status.events_elapsed_s = _events_elapsed_s(folder)
        status.event_agent_backend = _latest_event_backend(folder)

    return status


@dataclass
class TaskRow:
    """Per-task status for the ola-top parallel view.

    Sourced from ``<folder>/.ola/tasks.json`` (the spine: one row per tracked
    task) with the latest event per task folded in from
    ``<folder>/.ola/events.jsonl`` (``elapsed_s`` and ``last_progress_message``).
    Per-task token/turn metrics are folded in from ``STATS.jsonl`` rows whose
    phase matches ``task-<task_id>-<attempt>`` (``stats``, summed across the
    task's attempts). Additive to :class:`FolderStatus` — only used when
    ``.ola/`` is present.
    """

    task_id: str
    text: str
    status: str
    attempt: int = 0
    elapsed_s: float = 0.0
    last_progress_message: str = ""
    # Aggregate of this task's STATS.jsonl rows across all attempts. ``None``
    # when no matching row exists yet (e.g. a pending task that has not run).
    stats: IterationStatus | None = None


def _parse_ts(ts: str) -> datetime | None:
    """Parse an event ISO-8601 timestamp (trailing 'Z'), or None if malformed."""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


# A run that has emitted no event for this long is treated as dangling: an
# aborted ``ola`` invocation that never wrote its agents' terminal events. Past
# this window an open ("started" without "complete"/"failed") agent is no longer
# counted as running — the live clock freezes, the picker stops treating its
# folder as active, and gaps this long (e.g. between an aborted run and a re-run
# sharing one events.jsonl) are excluded from elapsed time. Generous versus the
# agents' event cadence (cc ~1/s, ct every ~5s) so a slow turn is never mistaken
# for a dead one.
_STALE_AFTER_S = 120.0


def _is_stale(dt: datetime | None, now: datetime) -> bool:
    """True when an event time is older than the staleness window.

    Defends against a naive/aware mismatch by treating an uncomparable ts as
    *fresh* — better to keep a live readout ticking than to wrongly freeze it.
    """
    if dt is None:
        return True
    try:
        return (now - dt).total_seconds() > _STALE_AFTER_S
    except TypeError:
        return False


def _worked_seconds(timestamps: list[datetime]) -> float:
    """Sum of gaps between consecutive (sorted) events, excluding any gap longer
    than :data:`_STALE_AFTER_S`.

    So a quiet stretch where nothing ran — most importantly the gap between an
    aborted run and a later re-run that share one ``events.jsonl`` — does not
    inflate a folder's elapsed time into a wall-clock span. Fewer than two
    timestamps yields 0.0.
    """
    ts = sorted(timestamps)
    return sum(
        d
        for a, b in zip(ts, ts[1:])
        if (d := (b - a).total_seconds()) <= _STALE_AFTER_S
    )


def _read_events(folder: Path) -> list[dict]:
    """Read ``<folder>/.ola/events.jsonl`` as a flat list in file (emission) order.

    Malformed lines are skipped so a partially-written events.jsonl never
    breaks the monitor.
    """
    events_file = folder / ".ola" / "events.jsonl"
    if not events_file.exists():
        return []
    records: list[dict] = []
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _read_progress(folder: Path) -> dict[str, dict]:
    """Read ``<folder>/.ola/metrics.jsonl`` into named progress series.

    Each line is one ``{"name", "ts", "value"}`` sample. Samples are grouped by
    ``name`` and returned as ``{name: {"value": <latest>, "series": [[ts, value],
    …]}}`` with the series in chronological (file) order, capped to the last
    :data:`_PROGRESS_SERIES_CAP` points. Malformed lines are skipped (same
    tolerance as :func:`_read_events`); an absent file returns ``{}``.

    This is a distinct vocabulary from the token-throughput ``metrics`` /
    ``Metrics`` plumbing — these are arbitrary user-emitted progress counters.
    """
    metrics_file = folder / ".ola" / "metrics.jsonl"
    if not metrics_file.exists():
        return {}
    series: dict[str, list[list]] = {}
    for line in metrics_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = sample.get("name")
        if name is None:
            continue
        series.setdefault(name, []).append([sample.get("ts"), sample.get("value")])
    return {
        name: {
            "value": points[-1][1],
            "series": points[-_PROGRESS_SERIES_CAP:],
        }
        for name, points in series.items()
    }


def _events_elapsed_s(folder: Path) -> float:
    """Worked time across ``<folder>/.ola/events.jsonl`` (gaps-excluded span).

    Recomputed on every read so an interrupted-then-resumed run reports its true
    elapsed time, rather than a stale ``STATS.jsonl``-derived number that can
    read shorter than a single task. Idle/dead stretches longer than
    :data:`_STALE_AFTER_S` — e.g. the gap between an aborted run and a re-run
    sharing this file — are excluded, so a dangling run can't inflate the span.
    Returns 0.0 with fewer than two parsable timestamps.
    """
    timestamps = [
        t for t in (_parse_ts(e.get("ts", "")) for e in _read_events(folder)) if t
    ]
    if len(timestamps) < 2:
        return 0.0
    return _worked_seconds(timestamps)


def _latest_event_backend(folder: Path) -> str:
    """Agent mnemonic from the latest event that carries one, or ``""``.

    ``agent_backend`` is in every event (incl. ``started``), so this surfaces
    the agent as soon as the first event lands — before any STATS.jsonl row.
    The latest event carrying one wins, matching how the dashboard derives the
    folder's running backend.
    """
    backend = ""
    for ev in _read_events(folder):
        if ev.get("agent_backend"):
            backend = ev["agent_backend"]
    return backend


def _read_events_by_task(folder: Path) -> dict[str, list[dict]]:
    """Group events.jsonl records by ``task_id`` in file (emission) order."""
    by_task: dict[str, list[dict]] = {}
    for record in _read_events(folder):
        task_id = record.get("task_id")
        if not task_id:
            continue
        by_task.setdefault(task_id, []).append(record)
    return by_task


def _aggregate_task_stats(iters: list[IterationStatus]) -> IterationStatus | None:
    """Sum a task's per-attempt STATS rows into one aggregate IterationStatus.

    Counters (tokens, turns, timing) are summed so the row shows the task's
    total cost across retries; ``max_input_tokens`` takes the max, ``ttft_ms``
    the median of streamed attempts, ``streamed`` is true only if every attempt
    streamed, and agent/models come from the latest attempt. Returns ``None``
    for an empty list so a never-run task carries no synthetic zeros.
    """
    if not iters:
        return None
    agg = IterationStatus(phase="")
    for it in iters:
        agg.input_tokens += it.input_tokens
        agg.output_tokens += it.output_tokens
        agg.cache_read_tokens += it.cache_read_tokens
        agg.cache_creation_tokens += it.cache_creation_tokens
        agg.num_turns += it.num_turns
        agg.wall_ms += it.wall_ms
        agg.tool_ms += it.tool_ms
        agg.llm_ms += it.llm_ms
        agg.max_input_tokens = max(agg.max_input_tokens, it.max_input_tokens)
    last = iters[-1]
    agg.agent = last.agent
    agg.agent_version = last.agent_version
    agg.streamed = all(it.streamed for it in iters)
    ttfts = [it.ttft_ms for it in iters if it.ttft_ms > 0]
    agg.ttft_ms = round(statistics.median(ttfts)) if ttfts else 0
    models: list[str] = []
    for it in iters:
        for m in it.models:
            if m and m not in models:
                models.append(m)
    agg.models = models
    return agg


def _task_iterations(
    iterations: list[IterationStatus], task_id: str
) -> list[IterationStatus]:
    """Return the iterations whose phase is ``task-<task_id>-<attempt>``.

    Matched as a literal ``task-<task_id>-`` prefix followed by an all-digit
    attempt, so collision-suffixed task ids (e.g. ``t-abc1234-2``) don't
    swallow a sibling's rows. Preserves file (append) order.
    """
    prefix = f"task-{task_id}-"
    return [
        it
        for it in iterations
        if it.phase.startswith(prefix) and it.phase[len(prefix) :].isdigit()
    ]


def read_task_rows(
    folder: Path, iterations: list[IterationStatus] | None = None
) -> list[TaskRow]:
    """Read per-task rows for a folder running in parallel mode.

    Returns one :class:`TaskRow` per task in ``<folder>/.ola/tasks.json``
    (PLAN.md order), folding in the latest event per task from
    ``<folder>/.ola/events.jsonl`` and, when ``iterations`` (the folder's
    parsed STATS.jsonl rows) are supplied, the per-task token/turn aggregate.
    Returns an empty list when the folder has no ``.ola/tasks.json`` (i.e. it
    is not running in parallel mode).
    """
    if not (folder / ".ola" / "tasks.json").exists():
        return []

    events_by_task = _read_events_by_task(folder)
    iterations = iterations or []

    rows: list[TaskRow] = []
    for entry in TaskState.load(folder).all():
        events = events_by_task.get(entry.task_id, [])

        # Worked time across the task's events, with dead gaps (e.g. an aborted
        # attempt that never closed before a re-run) excluded — see
        # :func:`_worked_seconds`.
        timestamps = [t for t in (_parse_ts(e.get("ts", "")) for e in events) if t]
        elapsed_s = _worked_seconds(timestamps)

        # Latest event carrying a progress message (working/complete/failed may
        # all attach one under data.message).
        last_message = ""
        for e in events:
            msg = e.get("data", {}).get("message")
            if msg:
                last_message = msg

        rows.append(
            TaskRow(
                task_id=entry.task_id,
                # Full task text — ola-top truncates per column (ellipsis) and
                # shows the whole value in its detail line, so the data layer
                # holds the truth and the display decides how much to show.
                text=entry.text,
                status=entry.status,
                attempt=entry.attempts,
                elapsed_s=elapsed_s,
                last_progress_message=last_message,
                stats=_aggregate_task_stats(
                    _task_iterations(iterations, entry.task_id)
                ),
            )
        )
    return rows


def read_agent_folder(agent_path: Path) -> list[FolderStatus]:
    """Read every plan subfolder of an agent directory and return their statuses.

    Subfolders are sorted by name. Skipped: hidden directories (starting with
    ``.``) and any directory without a ``PLAN.md`` — a folder with no plan is not
    a run ola-top can show task/metrics rows for, so it never appears in the list
    (mirrors the harness, which only drives folders that carry a ``PLAN.md``).
    """
    if not agent_path.is_dir():
        return []
    subfolders = sorted(
        p
        for p in agent_path.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and (p / "PLAN.md").exists()
    )
    return [read_folder_status(f) for f in subfolders]


# ---------------------------------------------------------------------------
# ola-dashboard snapshot
#
# The dashboard is a browser view over the same files ola-top reads; the
# ``ola-dashboard`` server re-parses the agent folder per request and returns
# ``build_snapshot`` as JSON. The shape mirrors what the old collector emitted
# (the dashboard's ``snapshot/types.ts`` ``Snapshot``), so the SPA needed no
# data-shape change — only its transport (SSE → polling) did.
# ---------------------------------------------------------------------------

# Recently-completed rows retained in the snapshot's activity feed. Matches the
# SPA's ACTIVITY_FEED_LIMIT — enough to fill a sidebar without unbounded growth.
_ACTIVITY_LIMIT = 50

# Per-named-series progress samples retained in the snapshot. Caps the sparkline
# history so a long-running task's metrics.jsonl can't grow the snapshot without
# bound; the latest value is always preserved separately.
_PROGRESS_SERIES_CAP = 60

# tasks.json spine status → dashboard lifecycle status, used only when a task
# has emitted no events yet (e.g. a checkbox ticked before any run). Once events
# exist, the latest event's status wins. The dashboard has no ``blocked`` state,
# so a blocked spine entry renders as ``failed`` (its checkbox is unticked, so
# it returns to the pool on the next claim — same as a failed attempt).
_SPINE_TO_LIFECYCLE: dict[str, str] = {
    "pending": "pending",
    "running": "working",
    "complete": "complete",
    "failed": "failed",
    "blocked": "failed",
}

_TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "failed"})


def build_snapshot(agent_path: Path, now: datetime | None = None) -> dict:
    """Build the dashboard snapshot for ``agent_path`` from the on-disk files.

    Two kinds of subfolder appear. A folder running in parallel mode (with
    ``.ola/tasks.json``) renders from its per-task spine: each task starts from
    its spine entry (``task_id``, text, attempts) and is enriched with its
    latest ``events.jsonl`` event for ``agent_backend``, ``data`` (latest
    payload, incl. ``metrics``), the finer lifecycle ``status``, and ``attempt``.
    A folder with only a ``PLAN.md`` (a future run the harness hasn't started)
    is seeded from its checkboxes as ``pending`` tasks so the picker can move to
    it before it begins. Stateless: every call re-reads the files, so a
    killed/restarted server loses nothing.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    tasks: dict[str, dict] = {}
    folders: dict[str, dict] = {}
    activity: list[dict] = []
    first_started_ts: str | None = None

    for folder in _dashboard_subfolders(agent_path):
        name = folder.name
        if not (folder / ".ola" / "tasks.json").exists():
            # Future run: a PLAN.md the harness hasn't started yet (no task
            # spine). Seed its checkboxes as pending so the picker can move to
            # it and preview the plan; it has no events, clock, or activity.
            _add_future_folder(folder, name, tasks, folders)
            continue
        events_by_task = _read_events_by_task(folder)

        folder_first: str | None = None
        folder_last_terminal: str | None = None
        # Dominant backend for the folder — the latest event carrying one wins,
        # so the header reflects whatever agent is actually running this run.
        folder_backend: str = ""

        for entry in TaskState.load(folder).all():
            events = events_by_task.get(entry.task_id, [])
            last = events[-1] if events else None

            if last is not None:
                status = last.get("status", "pending")
                agent_backend = last.get("agent_backend", "")
                attempt = int(last.get("attempt", entry.attempts))
                data = last.get("data") or {}
                # A non-terminal latest event that has gone stale means the
                # agent for that attempt died (an aborted run that never wrote a
                # terminal event). Fall back to the spine status — which the
                # harness keeps in lock-step with PLAN.md (checkbox-is-truth) —
                # so a dangling "working" task doesn't render as running forever.
                if status not in _TERMINAL_STATUSES and _is_stale(
                    _parse_ts(last.get("ts", "")), now
                ):
                    status = _SPINE_TO_LIFECYCLE.get(entry.status, "pending")
            else:
                status = _SPINE_TO_LIFECYCLE.get(entry.status, "pending")
                agent_backend = ""
                attempt = entry.attempts
                data = {}

            tasks[entry.task_id] = {
                "task_id": entry.task_id,
                "task_text": entry.text,
                "folder": name,
                "agent_backend": agent_backend,
                "status": status,
                "attempt": attempt,
                "data": data,
            }

        # Folder run clock + activity feed, scanned over the flat event stream
        # so manifest-order (tasks.json) and emission-order (events) stay
        # independent concerns.
        folder_events = _read_events(folder)
        for ev in folder_events:
            ts = ev.get("ts")
            ev_status = ev.get("status")
            if ev.get("agent_backend"):
                folder_backend = ev["agent_backend"]
            if not ts:
                continue
            if ev_status == "started" and (folder_first is None or ts < folder_first):
                folder_first = ts
            if ev_status in _TERMINAL_STATUSES and (
                folder_last_terminal is None or ts > folder_last_terminal
            ):
                folder_last_terminal = ts
            if ev_status == "complete":
                activity.append(
                    {
                        "task_id": ev.get("task_id", ""),
                        "task_text": ev.get("task_text", ""),
                        "folder": name,
                        "agent_backend": ev.get("agent_backend", ""),
                        "ts": ts,
                        "data": ev.get("data") or {},
                    }
                )

        active_elapsed_s, active_anchor_ts = _active_elapsed(folder_events, now)
        folders[name] = {
            "first_started_ts": folder_first,
            "last_terminal_ts": folder_last_terminal,
            "project": name,
            "agent_backend": folder_backend,
            # Model names come from STATS.jsonl (events don't carry them); the
            # header shows what the agent is actually driving.
            "models": _folder_models(folder),
            # Stopwatch that only runs while ≥1 agent is active: accumulated
            # active wall seconds, plus the ts to extrapolate the open tail from
            # (``None`` when currently idle, so the readout freezes).
            "active_elapsed_s": active_elapsed_s,
            "active_anchor_ts": active_anchor_ts,
            "progress": _read_progress(folder),
        }
        if folder_first is not None and (
            first_started_ts is None or folder_first < first_started_ts
        ):
            first_started_ts = folder_first

    # Newest-first, capped. RFC 3339 'Z' timestamps sort correctly as strings.
    activity.sort(key=lambda e: e["ts"], reverse=True)
    del activity[_ACTIVITY_LIMIT:]

    return {
        "first_started_ts": first_started_ts,
        "counters": _snapshot_counters(tasks),
        "tasks": tasks,
        "folders": folders,
        "activity": activity,
    }


def _folder_models(folder: Path) -> list[str]:
    """Unique model names a folder's STATS.jsonl reports, in first-seen order.

    Events never carry the model name, so the snapshot surfaces it from
    STATS.jsonl (the same file ola-top reads). A malformed/partially-written
    STATS.jsonl yields an empty list rather than breaking the snapshot.
    """
    stats_file = folder / "STATS.jsonl"
    if not stats_file.exists():
        return []
    try:
        iterations = parse_stats_jsonl(stats_file.read_text())
    except (json.JSONDecodeError, ValueError, KeyError):
        return []
    models: list[str] = []
    for it in iterations:
        for m in it.models:
            if m and m not in models:
                models.append(m)
    return models


def _active_elapsed(events: list[dict], now: datetime) -> tuple[float, str | None]:
    """Wall seconds with ≥1 agent active, plus the live-tail anchor ts.

    An attempt is active between its ``started`` and its terminal
    (``complete``/``failed``) event; ``working`` events leave the count
    unchanged. Walking the ts-sorted stream and tracking how many agents are
    concurrently active, this sums the time the count was >0 — idle gaps
    (count 0) are excluded, so the dashboard's elapsed readout is a stopwatch
    that runs only while work is happening. A gap longer than
    :data:`_STALE_AFTER_S` is also excluded even while the count is >0: an open
    agent that never closed across such a gap was a dead/aborted run, not work.

    The second return value is the ts of the last event when an agent is still
    running, so the consumer can tick the open interval out to "now". It is
    ``None`` when the run is idle (count 0) *or* when the last event is stale —
    i.e. an aborted run left an agent "running" forever — so the readout freezes
    instead of ticking up indefinitely.
    """
    timed = sorted(
        (
            (dt, e.get("status"), e.get("ts"))
            for e in events
            if (dt := _parse_ts(e.get("ts", ""))) is not None
        ),
        key=lambda it: it[0],
    )
    active_s = 0.0
    count = 0
    prev: datetime | None = None
    for dt, status, _ts in timed:
        if prev is not None and count > 0:
            gap = (dt - prev).total_seconds()
            if gap <= _STALE_AFTER_S:  # don't count time across a dead gap
                active_s += gap
        if status == "started":
            count += 1
        elif status in _TERMINAL_STATUSES:
            count = max(0, count - 1)
        prev = dt
    live = count > 0 and timed and not _is_stale(timed[-1][0], now)
    anchor = timed[-1][2] if live else None
    return active_s, anchor


def _add_future_folder(
    folder: Path,
    name: str,
    tasks: dict[str, dict],
    folders: dict[str, dict],
) -> None:
    """Seed a not-yet-started folder (a ``PLAN.md`` with no ``.ola/tasks.json``).

    Its checkbox tasks are surfaced as ``pending`` (``complete`` if already
    ticked) so the dashboard can preview an upcoming run before the harness
    seeds the spine. No events exist yet, so the folder clock is empty and the
    folder contributes nothing to the run's activity feed or elapsed stopwatch.
    """
    for t in enumerate_tasks(folder):
        tasks[t.task_id] = {
            "task_id": t.task_id,
            "task_text": t.text,
            "folder": name,
            "agent_backend": "",
            "status": "complete" if t.checked else "pending",
            "attempt": 0,
            "data": {},
        }
    folders[name] = {
        "first_started_ts": None,
        "last_terminal_ts": None,
        "project": name,
        "agent_backend": "",
        "models": _folder_models(folder),
        "active_elapsed_s": 0.0,
        "active_anchor_ts": None,
        # A future folder has no metrics file yet, so no progress series.
        "progress": {},
    }


def _dashboard_subfolders(agent_path: Path) -> list[Path]:
    """Sorted subfolders the dashboard surfaces.

    Folders running in parallel mode (``.ola/tasks.json``) — finished or live —
    plus folders that merely have a ``PLAN.md`` (a future run the harness has
    not started yet), so the project picker can move to a run before it begins.
    """
    if not agent_path.is_dir():
        return []
    return sorted(
        p
        for p in agent_path.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and ((p / ".ola" / "tasks.json").exists() or (p / "PLAN.md").exists())
    )


def _snapshot_counters(tasks: dict[str, dict]) -> dict:
    """Global task counters for the snapshot (the SPA recomputes per folder)."""
    completed = failed = active = 0
    for t in tasks.values():
        if t["status"] == "complete":
            completed += 1
        elif t["status"] == "failed":
            failed += 1
        elif t["status"] != "pending":
            active += 1
    return {
        "total_tasks": len(tasks),
        "completed": completed,
        "failed": failed,
        "active": active,
    }
