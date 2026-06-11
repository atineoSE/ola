"""Event emitter and sink abstraction for Ola events.

:class:`Emitter` is the harness-facing surface: it owns envelope assembly
(``ts`` and a monotonic ``seq`` per ``(agent_id, attempt)``) and fans each
assembled :class:`~ola.events.schema.Event` out to a set of sinks. Callers only supply the event-specific metadata via
:meth:`Emitter.started`, :meth:`Emitter.working`, :meth:`Emitter.complete`,
and :meth:`Emitter.failed`.

A :class:`Sink` is anything that can consume an assembled event.
:class:`LocalSink` mirrors every event as a JSON line in
``<folder>/.ola/events.jsonl`` via a single dedicated writer thread. It is
fire-and-forget: ``emit`` never blocks the caller for long and never raises
back into the scheduler.
"""

from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ola.events.schema import VALID_STATUSES, Event

logger = logging.getLogger(__name__)

# Sentinel pushed onto a sink's queue to tell its writer thread to drain and
# exit. A unique object so it can never collide with a real ``Event``.
_SHUTDOWN = object()


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


class LocalSink(Sink):
    """Appends each event as one JSON line to ``<folder>/.ola/events.jsonl``.

    A single dedicated daemon thread drains an unbounded queue and is the only
    writer of the file, so no file locks are needed — writes are serialized at
    the harness. :meth:`emit` only enqueues, so a worker is never blocked on
    disk I/O. Fire-and-forget: open/write errors are logged, never raised back
    to the caller.

    The queue is unbounded because local appends are cheap and we prefer not to
    drop a folder's own audit trail.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._queue: queue.Queue = queue.Queue()
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="ola-localsink", daemon=True
        )
        self._thread.start()

    def emit(self, event: Event) -> None:
        if self._closed.is_set():
            return
        self._queue.put(event)

    def _run(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("a", encoding="utf-8")
        except OSError:
            logger.exception("LocalSink: cannot open %s; dropping events", self._path)
            self._drain_until_shutdown()
            return
        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN:
                    return
                try:
                    handle.write(item.to_json() + "\n")
                    handle.flush()
                except OSError:
                    logger.exception("LocalSink: failed to write event")
        finally:
            handle.close()

    def _drain_until_shutdown(self) -> None:
        """Discard queued events until the shutdown sentinel arrives.

        Used when the file cannot be opened: producers must not block on a full
        queue (it is unbounded, but :meth:`close` still expects the thread to
        consume the sentinel and exit).
        """
        while self._queue.get() is not _SHUTDOWN:
            pass

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(_SHUTDOWN)
        self._thread.join(timeout=5.0)


class Emitter:
    """Assembles event envelopes and dispatches them to its sinks.

    The emitter is the single owner of envelope-level invariants:

    - ``ts`` is stamped at emit time in UTC millisecond ISO-8601.
    - ``seq`` is a monotonic counter scoped to each ``(agent_id, attempt)``
      pair, starting at ``0`` for the first event of that pair and
      incrementing by one per subsequent event. This lets a consumer order
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
