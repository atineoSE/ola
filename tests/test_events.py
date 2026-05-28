"""Tests for ola.events — envelope serialization and emitter assembly."""

from __future__ import annotations

import json
import re
import threading

import pytest

from ola.events import SCHEMA_VERSION, Emitter, Event, NullSink, Sink

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

_BASE = dict(
    agent_id="agent-0001",
    attempt=0,
    folder="09-parallel-agents",
    task_id="t-abc1234",
    task_text="Refactor extractor to use shared HTTP client",
    agent_backend="cc",
)


class _RecordingSink(Sink):
    """Captures every emitted event; thread-safe for concurrency tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._lock = threading.Lock()
        self.closed = False

    def emit(self, event: Event) -> None:
        with self._lock:
            self.events.append(event)

    def close(self) -> None:
        self.closed = True


def test_event_to_dict_round_trips_through_json() -> None:
    event = Event(
        schema_version=SCHEMA_VERSION,
        agent_id="agent-0042",
        attempt=0,
        seq=3,
        ts="2026-05-27T14:03:11.482Z",
        folder="09-parallel-agents",
        task_id="t-abc1234",
        task_text="Do the thing",
        agent_backend="cc",
        status="started",
        data={"k": "v"},
    )
    assert json.loads(event.to_json()) == event.to_dict()


def test_event_to_json_is_single_line() -> None:
    event = Event(
        schema_version=SCHEMA_VERSION,
        agent_id="a",
        attempt=0,
        seq=0,
        ts="2026-05-27T14:03:11.482Z",
        folder="f",
        task_id="t",
        task_text="multi\nline\ntext",
        agent_backend="cc",
        status="working",
    )
    assert "\n" not in event.to_json()


def test_emitter_stamps_schema_version_and_timestamp() -> None:
    emitter = Emitter([NullSink()])
    event = emitter.started(**_BASE)
    assert event.schema_version == SCHEMA_VERSION
    assert _TS_RE.match(event.ts), event.ts


def test_emitter_dispatches_to_all_sinks() -> None:
    s1, s2 = _RecordingSink(), _RecordingSink()
    emitter = Emitter([s1, s2])
    emitter.started(**_BASE)
    assert len(s1.events) == 1
    assert len(s2.events) == 1
    assert s1.events[0].status == "started"


def test_seq_is_monotonic_per_agent_attempt() -> None:
    sink = _RecordingSink()
    emitter = Emitter([sink])
    emitter.started(**_BASE)
    emitter.working(**_BASE)
    emitter.complete(**_BASE)
    seqs = [e.seq for e in sink.events]
    assert seqs == [0, 1, 2]


def test_seq_is_scoped_per_agent_attempt_pair() -> None:
    sink = _RecordingSink()
    emitter = Emitter([sink])
    emitter.started(**{**_BASE, "agent_id": "a", "attempt": 0})
    emitter.working(**{**_BASE, "agent_id": "a", "attempt": 0})
    emitter.started(**{**_BASE, "agent_id": "a", "attempt": 1})
    emitter.started(**{**_BASE, "agent_id": "b", "attempt": 0})
    by_key = {(e.agent_id, e.attempt): [] for e in sink.events}
    for e in sink.events:
        by_key[(e.agent_id, e.attempt)].append(e.seq)
    assert by_key[("a", 0)] == [0, 1]
    assert by_key[("a", 1)] == [0]
    assert by_key[("b", 0)] == [0]


def test_lifecycle_methods_set_their_status() -> None:
    sink = _RecordingSink()
    emitter = Emitter([sink])
    emitter.started(**_BASE)
    emitter.working(**_BASE)
    emitter.complete(**_BASE)
    emitter.failed(**_BASE)
    assert [e.status for e in sink.events] == [
        "started",
        "working",
        "complete",
        "failed",
    ]


def test_data_defaults_to_empty_dict() -> None:
    sink = _RecordingSink()
    emitter = Emitter([sink])
    emitter.started(**_BASE)
    assert sink.events[0].data == {}


def test_data_payload_passed_through_opaquely() -> None:
    sink = _RecordingSink()
    emitter = Emitter([sink])
    payload = {"message": "running tests", "n": 3}
    emitter.working(**_BASE, data=payload)
    assert sink.events[0].data == payload


def test_emitter_with_no_sinks_is_a_noop() -> None:
    emitter = Emitter()
    event = emitter.started(**_BASE)
    assert event.status == "started"


def test_close_closes_every_sink() -> None:
    s1, s2 = _RecordingSink(), _RecordingSink()
    emitter = Emitter([s1, s2])
    emitter.close()
    assert s1.closed and s2.closed


def test_concurrent_emits_allocate_unique_seqs() -> None:
    sink = _RecordingSink()
    emitter = Emitter([sink])

    def worker() -> None:
        for _ in range(50):
            emitter.working(**_BASE)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = sorted(e.seq for e in sink.events)
    assert seqs == list(range(8 * 50))


def test_invalid_status_rejected() -> None:
    emitter = Emitter()
    with pytest.raises(ValueError):
        emitter._emit(
            status="bogus",
            agent_id="a",
            attempt=0,
            folder="f",
            task_id="t",
            task_text="x",
            agent_backend="cc",
            data=None,
        )
