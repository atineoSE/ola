"""Data layer for the ola-top monitor: parse agent folders into status models."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ola.plan import parse_task_counts
from ola.stats import cache_hit_rate as _cache_hit_rate
from ola.taskstate import TaskState

# Max characters of task text retained in a TaskRow before truncating with "…".
_TASK_TEXT_MAX = 60


_AGENT_FULL_NAMES: dict[str, str] = {
    "cc": "Claude Code",
    "oh": "OpenHands",
    "cx": "Codex",
}


@dataclass
class IterationStatus:
    """Stats for a single iteration (seed or loop-N)."""

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

    @property
    def is_parallel(self) -> bool:
        """True when this folder runs in parallel mode (``.ola/`` present)."""
        return self.concurrency_cap is not None

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
        """Agent display from the most recent iteration."""
        if self.iterations:
            return self.iterations[-1].agent_display
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
        status.task_rows = read_task_rows(folder)

    return status


@dataclass
class TaskRow:
    """Per-task status for the ola-top parallel view.

    Sourced from ``<folder>/.ola/tasks.json`` (the spine: one row per tracked
    task) with the latest event per task folded in from
    ``<folder>/.ola/events.jsonl`` (``elapsed_s`` and ``last_progress_message``).
    Additive to :class:`FolderStatus` — only used when ``.ola/`` is present.
    """

    task_id: str
    text: str
    status: str
    attempt: int = 0
    elapsed_s: float = 0.0
    last_progress_message: str = ""


def _truncate(text: str, limit: int = _TASK_TEXT_MAX) -> str:
    """Truncate ``text`` to ``limit`` characters, appending '…' if cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _parse_ts(ts: str) -> datetime | None:
    """Parse an event ISO-8601 timestamp (trailing 'Z'), or None if malformed."""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


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


def _read_events_by_task(folder: Path) -> dict[str, list[dict]]:
    """Group events.jsonl records by ``task_id`` in file (emission) order."""
    by_task: dict[str, list[dict]] = {}
    for record in _read_events(folder):
        task_id = record.get("task_id")
        if not task_id:
            continue
        by_task.setdefault(task_id, []).append(record)
    return by_task


def read_task_rows(folder: Path) -> list[TaskRow]:
    """Read per-task rows for a folder running in parallel mode.

    Returns one :class:`TaskRow` per task in ``<folder>/.ola/tasks.json``
    (PLAN.md order), folding in the latest event per task from
    ``<folder>/.ola/events.jsonl``. Returns an empty list when the folder has
    no ``.ola/tasks.json`` (i.e. it is not running in parallel mode).
    """
    if not (folder / ".ola" / "tasks.json").exists():
        return []

    events_by_task = _read_events_by_task(folder)

    rows: list[TaskRow] = []
    for entry in TaskState.load(folder).all():
        events = events_by_task.get(entry.task_id, [])

        elapsed_s = 0.0
        timestamps = [t for t in (_parse_ts(e.get("ts", "")) for e in events) if t]
        if len(timestamps) >= 2:
            elapsed_s = (max(timestamps) - min(timestamps)).total_seconds()

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
                text=_truncate(entry.text),
                status=entry.status,
                attempt=entry.attempts,
                elapsed_s=elapsed_s,
                last_progress_message=last_message,
            )
        )
    return rows


def read_agent_folder(agent_path: Path) -> list[FolderStatus]:
    """Read all subfolders of an agent directory and return their statuses.

    Subfolders are sorted by name. Hidden directories (starting with .) are skipped.
    """
    if not agent_path.is_dir():
        return []
    subfolders = sorted(
        p for p in agent_path.iterdir() if p.is_dir() and not p.name.startswith(".")
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


def build_snapshot(agent_path: Path) -> dict:
    """Build the dashboard snapshot for ``agent_path`` from the on-disk files.

    Only parallel-mode subfolders (those with ``.ola/tasks.json``) appear —
    they are the per-task spine the dashboard renders. Each task starts from
    its spine entry (``task_id``, text, attempts) and is enriched with its
    latest ``events.jsonl`` event for ``agent_backend``, ``data`` (latest
    payload, incl. ``metrics``), the finer lifecycle ``status``, and ``attempt``.
    Stateless: every call re-reads the files, so a killed/restarted server
    loses nothing.
    """
    tasks: dict[str, dict] = {}
    folders: dict[str, dict] = {}
    activity: list[dict] = []
    first_started_ts: str | None = None

    for folder in _parallel_subfolders(agent_path):
        name = folder.name
        events_by_task = _read_events_by_task(folder)

        folder_first: str | None = None
        folder_last_terminal: str | None = None

        for entry in TaskState.load(folder).all():
            events = events_by_task.get(entry.task_id, [])
            last = events[-1] if events else None

            if last is not None:
                status = last.get("status", "pending")
                agent_backend = last.get("agent_backend", "")
                attempt = int(last.get("attempt", entry.attempts))
                data = last.get("data") or {}
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
        for ev in _read_events(folder):
            ts = ev.get("ts")
            ev_status = ev.get("status")
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

        folders[name] = {
            "first_started_ts": folder_first,
            "last_terminal_ts": folder_last_terminal,
            "project": name,
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


def _parallel_subfolders(agent_path: Path) -> list[Path]:
    """Sorted subfolders of ``agent_path`` running in parallel mode (``.ola/``)."""
    if not agent_path.is_dir():
        return []
    return sorted(
        p
        for p in agent_path.iterdir()
        if p.is_dir()
        and not p.name.startswith(".")
        and (p / ".ola" / "tasks.json").exists()
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
