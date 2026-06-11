"""Per-task state persistence for parallel agent runs.

Mirrors the checkboxes in ``<folder>/PLAN.md`` into a sidecar file
``<folder>/.ola/tasks.json`` so the scheduler can track per-task lifecycle
(pending / running / complete / failed), attempt counts, and last error
across runs and across worker threads.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ola.plan import enumerate_tasks

_VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "complete", "failed", "blocked"}
)


@dataclass
class TaskEntry:
    """A single tracked task. Mutable — TaskState updates fields in place."""

    task_id: str
    text: str
    line_no: int
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None


class TaskState:
    """Per-folder task tracker backed by ``<folder>/.ola/tasks.json``."""

    def __init__(self, folder: Path) -> None:
        self._folder = Path(folder)
        self._entries: dict[str, TaskEntry] = {}

    @staticmethod
    def _path(folder: Path) -> Path:
        return Path(folder) / ".ola" / "tasks.json"

    @classmethod
    def load(cls, folder: Path) -> TaskState:
        """Load tasks.json from ``<folder>/.ola/``; empty state if file is absent."""
        state = cls(folder)
        path = cls._path(folder)
        if not path.exists():
            return state
        raw = json.loads(path.read_text())
        for entry in raw.get("tasks", []):
            te = TaskEntry(**entry)
            if te.status not in _VALID_STATUSES:
                raise ValueError(
                    f"tasks.json contains invalid status {te.status!r} for {te.task_id!r}"
                )
            state._entries[te.task_id] = te
        return state

    @classmethod
    def sync_from_plan(cls, folder: Path) -> TaskState:
        """Load tasks.json (if any) and reconcile with PLAN.md.

        For task ids that exist in both, status/attempts/last_error are preserved
        and only text/line_no are refreshed from PLAN.md. For task ids in PLAN.md
        that are new, a pending entry is created (or complete if the checkbox is
        already ticked). For task ids in tasks.json that no longer appear in
        PLAN.md, entries are dropped.

        Crash recovery: a ``running`` status read from disk is stale — a fresh
        process has nothing actually in flight — so it is reset to ``pending``
        and its attempt count is rolled back by one (the dispatch loop
        re-increments on the next try), so a worker killed mid-attempt is
        retried without that interrupted attempt counting toward
        ``--max-attempts``. Unlike :meth:`resync` (mid-run, where ``running``
        means a live worker and must be preserved), this only runs at startup.
        """
        existing = cls.load(folder)
        synced = cls(folder)
        for t in enumerate_tasks(folder):
            prior = existing._entries.get(t.task_id)
            if prior is not None:
                prior.text = t.text
                prior.line_no = t.line_no
                if prior.status == "running":
                    prior.status = "pending"
                    prior.attempts = max(0, prior.attempts - 1)
                synced._entries[t.task_id] = prior
            else:
                synced._entries[t.task_id] = TaskEntry(
                    task_id=t.task_id,
                    text=t.text,
                    line_no=t.line_no,
                    status="complete" if t.checked else "pending",
                )
        return synced

    def resync(self) -> None:
        """Re-read PLAN.md and reconcile the entries **in place**.

        Used after a janitor edits the live PLAN.md mid-run. Unlike
        :meth:`sync_from_plan` this mutates the existing instance, because
        in-flight workers hold a reference to it. New checkbox lines become
        pending entries; lines that disappeared are dropped — except entries
        still ``running``, which are preserved so an in-flight worker's
        :meth:`mark` cannot raise. Caller must hold the state lock.
        """
        entries: dict[str, TaskEntry] = {}
        for t in enumerate_tasks(self._folder):
            prior = self._entries.get(t.task_id)
            if prior is not None:
                prior.text = t.text
                prior.line_no = t.line_no
                entries[t.task_id] = prior
            else:
                entries[t.task_id] = TaskEntry(
                    task_id=t.task_id,
                    text=t.text,
                    line_no=t.line_no,
                    status="complete" if t.checked else "pending",
                )
        for task_id, entry in self._entries.items():
            if task_id not in entries and entry.status == "running":
                entries[task_id] = entry
        self._entries = entries

    def mark(self, task_id: str, status: str, **kwargs: Any) -> None:
        """Set ``status`` (and optional extra fields) on the entry for ``task_id``.

        Extra kwargs may include ``attempts`` and ``last_error``. Unknown fields
        raise AttributeError; invalid statuses raise ValueError.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        entry = self._entries.get(task_id)
        if entry is None:
            raise KeyError(f"Unknown task_id: {task_id!r}")
        entry.status = status
        for key, value in kwargs.items():
            if not hasattr(entry, key):
                raise AttributeError(f"TaskEntry has no field {key!r}")
            setattr(entry, key, value)

    def save(self) -> None:
        """Atomically write tasks.json (tmp file + rename)."""
        path = self._path(self._folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tasks": [asdict(e) for e in self._entries.values()]}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)

    def get(self, task_id: str) -> TaskEntry | None:
        return self._entries.get(task_id)

    def all(self) -> list[TaskEntry]:
        """Return all entries in insertion order (matches PLAN.md order after sync)."""
        return list(self._entries.values())

    def next_pending(self) -> TaskEntry | None:
        """Return the first entry whose status is ``"pending"``, or ``None``.

        Used by the scheduler to pick the next task to dispatch. Iteration
        order follows PLAN.md (insertion order after ``sync_from_plan``).
        """
        for entry in self._entries.values():
            if entry.status == "pending":
                return entry
        return None
