import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCollectorStream } from "./useCollectorStream";
import type { LifecycleEvent, Snapshot, TaskState } from "./types";

/**
 * Minimal EventSource stand-in: supports addEventListener/removeEventListener
 * for the named events the hook listens to (`open`, `snapshot`, `event`,
 * `error`) plus a `close()` method and a test-only `emit()` to drive
 * deliveries from the test body.
 */
class FakeEventSource {
  static instances: FakeEventSource[] = [];

  url: string;
  closed = false;
  private listeners: Record<string, Set<(ev: Event | MessageEvent) => void>> = {};

  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (ev: Event | MessageEvent) => void) {
    (this.listeners[type] ??= new Set()).add(listener);
  }

  removeEventListener(type: string, listener: (ev: Event | MessageEvent) => void) {
    this.listeners[type]?.delete(listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: "open" | "error"): void;
  emit(type: "snapshot" | "event", data: unknown): void;
  emit(type: string, data?: unknown) {
    const set = this.listeners[type];
    if (!set) return;
    const ev =
      data === undefined
        ? new Event(type)
        : new MessageEvent(type, { data: JSON.stringify(data) });
    for (const listener of set) listener(ev);
  }
}

function snapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    first_started_ts: null,
    counters: { total_tasks: 0, completed: 0, failed: 0, active: 0 },
    tasks: {},
    ...overrides,
  };
}

function task(overrides: Partial<TaskState> = {}): TaskState {
  return {
    task_id: "t-abc1234",
    task_text: "Refactor extractor",
    folder: "09-parallel-agents",
    agent_backend: "cc",
    status: "started",
    attempt: 0,
    data: {},
    ...overrides,
  };
}

function event(overrides: Partial<LifecycleEvent> = {}): LifecycleEvent {
  return {
    agent_id: "agent-0001",
    attempt: 0,
    seq: 0,
    ts: "2026-05-27T14:03:11.482Z",
    folder: "09-parallel-agents",
    task_id: "t-abc1234",
    task_text: "Refactor extractor",
    agent_backend: "cc",
    status: "started",
    data: {},
    ...overrides,
  };
}

afterEach(() => {
  FakeEventSource.instances = [];
  vi.restoreAllMocks();
});

describe("useCollectorStream", () => {
  it("starts idle when no collectorUrl is given", () => {
    const { result } = renderHook(() => useCollectorStream(null));
    expect(result.current.status).toBe("idle");
    expect(result.current.store.counters.total_tasks).toBe(0);
  });

  it("opens an EventSource at `{collectorUrl}/stream`", () => {
    renderHook(() =>
      useCollectorStream("http://localhost:8000", {
        eventSourceCtor: FakeEventSource as unknown as typeof EventSource,
      }),
    );
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe(
      "http://localhost:8000/stream",
    );
  });

  it("appends `stream` to a collectorUrl with a trailing path", () => {
    renderHook(() =>
      useCollectorStream("https://example.com/api/", {
        eventSourceCtor: FakeEventSource as unknown as typeof EventSource,
      }),
    );
    expect(FakeEventSource.instances[0].url).toBe(
      "https://example.com/api/stream",
    );
  });

  it("transitions to `open` and seeds the store from the snapshot event", () => {
    const { result } = renderHook(() =>
      useCollectorStream("http://localhost:8000", {
        eventSourceCtor: FakeEventSource as unknown as typeof EventSource,
      }),
    );
    expect(result.current.status).toBe("connecting");

    const es = FakeEventSource.instances[0];
    act(() => es.emit("open"));
    expect(result.current.status).toBe("open");

    act(() =>
      es.emit(
        "snapshot",
        snapshot({
          first_started_ts: "2026-05-27T14:03:11.482Z",
          counters: { total_tasks: 2, completed: 1, failed: 0, active: 1 },
          tasks: {
            "t-a": task({
              task_id: "t-a",
              status: "complete",
              data: { duration_s: 4.2 },
            }),
            "t-b": task({ task_id: "t-b", status: "working" }),
          },
        }),
      ),
    );

    expect(result.current.store.counters.total_tasks).toBe(2);
    expect(result.current.store.first_started_ts).toBe(
      "2026-05-27T14:03:11.482Z",
    );
    expect(result.current.store.tasks["t-a"].data).toEqual({ duration_s: 4.2 });
  });

  it("folds subsequent `event` messages into the store", () => {
    const { result } = renderHook(() =>
      useCollectorStream("http://localhost:8000", {
        eventSourceCtor: FakeEventSource as unknown as typeof EventSource,
      }),
    );
    const es = FakeEventSource.instances[0];
    act(() => es.emit("snapshot", snapshot()));
    act(() => es.emit("event", event({ status: "started", seq: 0 })));
    act(() =>
      es.emit(
        "event",
        event({ status: "working", seq: 1, data: { errors: 7 } }),
      ),
    );

    expect(result.current.store.counters.total_tasks).toBe(1);
    expect(result.current.store.counters.active).toBe(1);
    expect(result.current.store.tasks["t-abc1234"].status).toBe("working");
    expect(result.current.store.tasks["t-abc1234"].data).toEqual({ errors: 7 });
  });

  it("transitions to `closed` on EventSource error", () => {
    const { result } = renderHook(() =>
      useCollectorStream("http://localhost:8000", {
        eventSourceCtor: FakeEventSource as unknown as typeof EventSource,
      }),
    );
    const es = FakeEventSource.instances[0];
    act(() => es.emit("error"));
    expect(result.current.status).toBe("closed");
  });

  it("closes the EventSource on unmount", () => {
    const { unmount } = renderHook(() =>
      useCollectorStream("http://localhost:8000", {
        eventSourceCtor: FakeEventSource as unknown as typeof EventSource,
      }),
    );
    const es = FakeEventSource.instances[0];
    expect(es.closed).toBe(false);
    unmount();
    expect(es.closed).toBe(true);
  });

  it("tears down the old connection and resets state when collectorUrl changes", () => {
    const { result, rerender } = renderHook(
      ({ url }: { url: string }) =>
        useCollectorStream(url, {
          eventSourceCtor: FakeEventSource as unknown as typeof EventSource,
        }),
      { initialProps: { url: "http://a/" } },
    );
    const first = FakeEventSource.instances[0];
    act(() => first.emit("snapshot", snapshot()));
    act(() => first.emit("event", event({ status: "started", seq: 0 })));
    expect(result.current.store.counters.total_tasks).toBe(1);

    rerender({ url: "http://b/" });
    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toBe("http://b/stream");
    expect(result.current.store.counters.total_tasks).toBe(0);
  });
});
