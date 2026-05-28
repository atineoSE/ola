"""Tests for ola.events — envelope serialization and emitter assembly."""

from __future__ import annotations

import json
import re
import threading
import time

import pytest

from ola.events import (
    SCHEMA_VERSION,
    Emitter,
    Event,
    HttpSink,
    LocalSink,
    NullSink,
    Sink,
)

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


# --- LocalSink ------------------------------------------------------------


def _make_event(seq: int = 0, status: str = "started") -> Event:
    return Event(
        schema_version=SCHEMA_VERSION,
        agent_id="agent-0001",
        attempt=0,
        seq=seq,
        ts="2026-05-27T14:03:11.482Z",
        folder="09-parallel-agents",
        task_id="t-abc1234",
        task_text="Do the thing",
        agent_backend="cc",
        status=status,
    )


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_localsink_writes_jsonl_lines(tmp_path) -> None:
    path = tmp_path / ".ola" / "events.jsonl"
    sink = LocalSink(path)
    try:
        sink.emit(_make_event(0, "started"))
        sink.emit(_make_event(1, "working"))
        sink.emit(_make_event(2, "complete"))
    finally:
        sink.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [p["status"] for p in parsed] == ["started", "working", "complete"]
    assert [p["seq"] for p in parsed] == [0, 1, 2]


def test_localsink_creates_parent_directory(tmp_path) -> None:
    path = tmp_path / "deep" / "nested" / ".ola" / "events.jsonl"
    sink = LocalSink(path)
    sink.emit(_make_event())
    sink.close()
    assert path.exists()


def test_localsink_appends_across_instances(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    first = LocalSink(path)
    first.emit(_make_event(0))
    first.close()

    second = LocalSink(path)
    second.emit(_make_event(1))
    second.close()

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_localsink_emit_after_close_is_dropped(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    sink = LocalSink(path)
    sink.emit(_make_event(0))
    sink.close()
    sink.emit(_make_event(1))  # must not raise, must not write
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_localsink_close_is_idempotent(tmp_path) -> None:
    sink = LocalSink(tmp_path / "events.jsonl")
    sink.close()
    sink.close()  # second close must be a no-op, not raise


# --- HttpSink -------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Records POSTs; thread-safe. Substitutes for ``httpx.Client``."""

    def __init__(self, *, status_code: int = 200, raises: bool = False) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.closed = False
        self._status = status_code
        self._raises = raises
        self._lock = threading.Lock()

    def post(self, url: str, json: dict):  # noqa: A002 - mirror httpx signature
        with self._lock:
            self.posts.append((url, json))
        if self._raises:
            raise RuntimeError("boom")
        return _FakeResponse(self._status)

    def close(self) -> None:
        self.closed = True


def _patch_httpx_client(monkeypatch, client: _FakeClient) -> None:
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)


def test_httpsink_posts_to_events_endpoint(monkeypatch) -> None:
    fake = _FakeClient()
    _patch_httpx_client(monkeypatch, fake)
    sink = HttpSink("http://collector.test")
    try:
        ev = _make_event()
        sink.emit(ev)
        assert _wait_for(lambda: len(fake.posts) == 1)
    finally:
        sink.close()

    url, body = fake.posts[0]
    assert url == "http://collector.test/events"
    assert body == ev.to_dict()


def test_httpsink_strips_trailing_slash(monkeypatch) -> None:
    fake = _FakeClient()
    _patch_httpx_client(monkeypatch, fake)
    sink = HttpSink("http://collector.test/")
    try:
        sink.emit(_make_event())
        assert _wait_for(lambda: len(fake.posts) == 1)
    finally:
        sink.close()
    assert fake.posts[0][0] == "http://collector.test/events"


def test_httpsink_close_closes_client(monkeypatch) -> None:
    fake = _FakeClient()
    _patch_httpx_client(monkeypatch, fake)
    sink = HttpSink("http://collector.test")
    sink.close()
    assert fake.closed


def test_httpsink_non_2xx_does_not_raise(monkeypatch) -> None:
    fake = _FakeClient(status_code=503)
    _patch_httpx_client(monkeypatch, fake)
    sink = HttpSink("http://collector.test")
    try:
        sink.emit(_make_event())
        assert _wait_for(lambda: len(fake.posts) == 1)
    finally:
        sink.close()  # must complete cleanly despite the 503


def test_httpsink_transport_error_does_not_raise(monkeypatch) -> None:
    fake = _FakeClient(raises=True)
    _patch_httpx_client(monkeypatch, fake)
    sink = HttpSink("http://collector.test")
    try:
        sink.emit(_make_event())
        assert _wait_for(lambda: len(fake.posts) == 1)
    finally:
        sink.close()


def test_httpsink_emit_after_close_is_dropped(monkeypatch) -> None:
    fake = _FakeClient()
    _patch_httpx_client(monkeypatch, fake)
    sink = HttpSink("http://collector.test")
    sink.close()
    sink.emit(_make_event())
    assert fake.posts == []


def test_httpsink_drops_oldest_on_overflow(monkeypatch) -> None:
    # A client whose posts block until released, so the queue fills up and the
    # drop-oldest path is exercised deterministically.
    release = threading.Event()
    in_post = threading.Event()
    seen: list[int] = []
    lock = threading.Lock()

    class _BlockingClient:
        def post(self, url: str, json: dict):  # noqa: A002
            in_post.set()
            release.wait(timeout=5.0)
            with lock:
                seen.append(json["seq"])
            return _FakeResponse(200)

        def close(self) -> None:
            pass

    import httpx

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _BlockingClient())

    sink = HttpSink("http://collector.test", max_queue=2)
    try:
        # First emit is pulled by the worker, which then blocks inside post();
        # the queue is now empty and the worker won't drain it until released.
        sink.emit(_make_event(0))
        assert _wait_for(in_post.is_set), "worker did not pick up the first event"
        # Fill the bounded queue (capacity 2) and overflow it; the oldest queued
        # events are dropped so only the newest survive.
        for seq in range(1, 6):
            sink.emit(_make_event(seq))
        release.set()
        # The in-flight event (0) plus at most max_queue (2) survivors post.
        assert _wait_for(lambda: len(seen) >= 3)
    finally:
        release.set()
        sink.close()

    with lock:
        # seq 0 was already in-flight; the queue could retain only the two
        # most-recent of seqs 1..5, so the newest (5) survives and early ones
        # were dropped (total posted is bounded).
        assert 0 in seen
        assert 5 in seen
        assert len(seen) <= 3
