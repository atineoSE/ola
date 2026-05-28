"""Event emitter and sink abstraction for Ola v2 events.

:class:`Emitter` is the harness-facing surface: it owns envelope assembly
(``schema_version``, ``ts``, and a monotonic ``seq`` per ``(agent_id,
attempt)``) and fans each assembled :class:`~ola.events.schema.Event` out to a
set of sinks. Callers only supply the event-specific metadata via
:meth:`Emitter.started`, :meth:`Emitter.working`, :meth:`Emitter.complete`,
and :meth:`Emitter.failed`.

A :class:`Sink` is anything that can consume an assembled event. The concrete
``LocalSink`` (JSONL file) and ``HttpSink`` (collector POST) implementations
land in the next task; this module defines the interface plus a no-op default
so the emitter is usable and testable on its own.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ola.events.schema import SCHEMA_VERSION, VALID_STATUSES, Event


def _now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDThh:mm:ss.sssZ``.

    Millisecond precision with a literal ``Z`` suffix, matching the envelope
    examples in SCHEMA.md.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class Sink(ABC):
    """Consumes assembled events. Implementations must be fire-and-forget.

    ``emit`` is called from worker threads and must never raise back into the
    emitter or block the caller for long — a misbehaving sink must not stall
    the scheduler. ``close`` flushes and releases resources at shutdown.
    """

    @abstractmethod
    def emit(self, event: Event) -> None: ...

    def close(self) -> None:
        """Flush and release resources. Default no-op; override as needed."""


class NullSink(Sink):
    """Discards every event. Useful as an explicit no-op in tests."""

    def emit(self, event: Event) -> None:  # noqa: D102 - trivial
        return None


class Emitter:
    """Assembles v2 envelopes and dispatches them to its sinks.

    The emitter is the single owner of envelope-level invariants:

    - ``schema_version`` is always :data:`~ola.events.schema.SCHEMA_VERSION`.
    - ``ts`` is stamped at emit time in UTC millisecond ISO-8601.
    - ``seq`` is a monotonic counter scoped to each ``(agent_id, attempt)``
      pair, starting at ``0`` for the first event of that pair and
      incrementing by one per subsequent event. This lets a collector order
      and gap-detect a single attempt's stream even if events arrive out of
      order.

    Thread-safe: ``seq`` allocation is guarded by a lock so concurrent workers
    emitting for distinct attempts never race. Sinks receive events serialized
    only by their own contract — the emitter holds no lock across ``sink.emit``.
    """

    def __init__(self, sinks: list[Sink] | None = None) -> None:
        self._sinks: list[Sink] = list(sinks) if sinks else []
        self._seq: dict[tuple[str, int], int] = {}
        self._seq_lock = threading.Lock()

    def _next_seq(self, agent_id: str, attempt: int) -> int:
        key = (agent_id, attempt)
        with self._seq_lock:
            seq = self._seq.get(key, 0)
            self._seq[key] = seq + 1
        return seq

    def _emit(
        self,
        *,
        status: str,
        agent_id: str,
        attempt: int,
        folder: str,
        task_id: str,
        task_text: str,
        agent_backend: str,
        data: dict[str, Any] | None,
    ) -> Event:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid event status: {status!r}")
        event = Event(
            schema_version=SCHEMA_VERSION,
            agent_id=agent_id,
            attempt=attempt,
            seq=self._next_seq(agent_id, attempt),
            ts=_now_iso(),
            folder=folder,
            task_id=task_id,
            task_text=task_text,
            agent_backend=agent_backend,
            status=status,
            data=data or {},
        )
        for sink in self._sinks:
            sink.emit(event)
        return event

    def started(
        self,
        *,
        agent_id: str,
        attempt: int,
        folder: str,
        task_id: str,
        task_text: str,
        agent_backend: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        """Emit the lifecycle-opening ``started`` event for an attempt."""
        return self._emit(
            status="started",
            agent_id=agent_id,
            attempt=attempt,
            folder=folder,
            task_id=task_id,
            task_text=task_text,
            agent_backend=agent_backend,
            data=data,
        )

    def working(
        self,
        *,
        agent_id: str,
        attempt: int,
        folder: str,
        task_id: str,
        task_text: str,
        agent_backend: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        """Emit a coarse-grained ``working`` progress event (may repeat)."""
        return self._emit(
            status="working",
            agent_id=agent_id,
            attempt=attempt,
            folder=folder,
            task_id=task_id,
            task_text=task_text,
            agent_backend=agent_backend,
            data=data,
        )

    def complete(
        self,
        *,
        agent_id: str,
        attempt: int,
        folder: str,
        task_id: str,
        task_text: str,
        agent_backend: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        """Emit the terminal ``complete`` event for a successful attempt."""
        return self._emit(
            status="complete",
            agent_id=agent_id,
            attempt=attempt,
            folder=folder,
            task_id=task_id,
            task_text=task_text,
            agent_backend=agent_backend,
            data=data,
        )

    def failed(
        self,
        *,
        agent_id: str,
        attempt: int,
        folder: str,
        task_id: str,
        task_text: str,
        agent_backend: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        """Emit the terminal ``failed`` event for a failed attempt."""
        return self._emit(
            status="failed",
            agent_id=agent_id,
            attempt=attempt,
            folder=folder,
            task_id=task_id,
            task_text=task_text,
            agent_backend=agent_backend,
            data=data,
        )

    def close(self) -> None:
        """Close every sink. Idempotent at the emitter level."""
        for sink in self._sinks:
            sink.close()
