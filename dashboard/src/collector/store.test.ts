import { describe, expect, it } from "vitest";

import {
  ACTIVITY_FEED_LIMIT,
  EMPTY_STORE,
  applyEvent,
  applyManifest,
  outputTokensPerSec,
  fromSnapshot,
  type CollectorStore,
} from "./store";
import type { LifecycleEvent, Snapshot, TaskState } from "./types";

function event(overrides: Partial<LifecycleEvent> = {}): LifecycleEvent {
  return {
    agent_id: "agent-0001",
    attempt: 0,
    seq: 0,
    ts: "2026-05-27T14:03:11.482Z",
    folder: "09-parallel-agents",
    task_id: "t-abc1234",
    task_text: "Refactor extractor to use shared HTTP client",
    agent_backend: "cc",
    status: "started",
    data: {},
    ...overrides,
  };
}

function taskState(overrides: Partial<TaskState> = {}): TaskState {
  return {
    task_id: "t-abc1234",
    task_text: "Refactor extractor to use shared HTTP client",
    folder: "09-parallel-agents",
    agent_backend: "cc",
    status: "started",
    attempt: 0,
    data: {},
    ...overrides,
  };
}

function run(events: LifecycleEvent[]): CollectorStore {
  return events.reduce(applyEvent, EMPTY_STORE);
}

describe("fromSnapshot", () => {
  it("seeds the store with the snapshot's counters and tasks", () => {
    const snapshot: Snapshot = {
      first_started_ts: "2026-05-27T14:03:11.482Z",
      counters: { total_tasks: 1, completed: 1, failed: 0, active: 0 },
      tasks: {
        "t-abc1234": taskState({
          status: "complete",
          data: { duration_s: 7.5, errors: 3 },
        }),
      },
    };
    const store = fromSnapshot(snapshot);
    expect(store.first_started_ts).toBe("2026-05-27T14:03:11.482Z");
    expect(store.counters).toEqual(snapshot.counters);
    expect(store.tasks).toEqual(snapshot.tasks);
    expect(store.maxSeq).toEqual({});
  });

  it("resets the activity feed (snapshot carries no completion order)", () => {
    const seeded = applyEvent(
      EMPTY_STORE,
      event({ status: "complete", data: { duration_s: 1.0 } }),
    );
    expect(seeded.activity).toHaveLength(1);
    const snapshot: Snapshot = {
      first_started_ts: null,
      counters: { total_tasks: 0, completed: 0, failed: 0, active: 0 },
      tasks: {},
    };
    expect(fromSnapshot(snapshot).activity).toEqual([]);
  });
});

describe("applyEvent — folding lifecycle into state", () => {
  it("started → working → complete tracks the full lifecycle", () => {
    const store = run([
      event({ status: "started", seq: 0 }),
      event({ status: "working", seq: 1, data: { message: "editing" } }),
      event({ status: "complete", seq: 2, data: { duration_s: 7.5 } }),
    ]);
    expect(store.first_started_ts).toBe("2026-05-27T14:03:11.482Z");
    expect(store.tasks["t-abc1234"]).toEqual(
      taskState({ status: "complete", data: { duration_s: 7.5 } }),
    );
    expect(store.counters).toEqual({
      total_tasks: 1,
      completed: 1,
      failed: 0,
      active: 0,
    });
  });

  it("retains the latest opaque data payload verbatim", () => {
    const store = run([
      event({ status: "started", seq: 0 }),
      event({ status: "working", seq: 1, data: { coverage_pct: 35 } }),
      event({ status: "working", seq: 2, data: { coverage_pct: 92 } }),
    ]);
    expect(store.tasks["t-abc1234"].data).toEqual({ coverage_pct: 92 });
  });

  it("surfaces agent_backend and folder from the envelope", () => {
    const store = run([
      event({ status: "started", agent_backend: "oh", folder: "07-foo" }),
    ]);
    expect(store.tasks["t-abc1234"].agent_backend).toBe("oh");
    expect(store.tasks["t-abc1234"].folder).toBe("07-foo");
  });

  it("first_started_ts is anchored to the first started event only", () => {
    const store = run([
      event({ status: "started", seq: 0, ts: "2026-05-27T14:03:11.482Z" }),
      event({
        status: "started",
        seq: 1,
        task_id: "t-other",
        agent_id: "agent-0002",
        ts: "2026-05-27T14:03:12.000Z",
      }),
    ]);
    expect(store.first_started_ts).toBe("2026-05-27T14:03:11.482Z");
  });

  it("drops duplicate seq from same (agent_id, attempt)", () => {
    const e = event({ status: "started", seq: 5 });
    const first = applyEvent(EMPTY_STORE, e);
    const second = applyEvent(first, e);
    expect(second).toBe(first);
  });

  it("drops out-of-order seq from same (agent_id, attempt)", () => {
    const a = event({ status: "working", seq: 3, data: { errors: 1 } });
    const b = event({ status: "started", seq: 2 });
    const after = applyEvent(applyEvent(EMPTY_STORE, a), b);
    expect(after.tasks["t-abc1234"].status).toBe("working");
    expect(after.tasks["t-abc1234"].data).toEqual({ errors: 1 });
  });

  it("treats different attempts as independent seq spaces", () => {
    const store = run([
      event({ status: "started", attempt: 0, seq: 5 }),
      event({
        status: "working",
        attempt: 1,
        seq: 0,
        data: { errors: 1 },
      }),
    ]);
    expect(store.tasks["t-abc1234"].attempt).toBe(1);
    expect(store.tasks["t-abc1234"].data).toEqual({ errors: 1 });
  });

  it("counts a failed task as failed, not active", () => {
    const store = run([
      event({ status: "started", seq: 0 }),
      event({
        status: "failed",
        seq: 1,
        data: { reason: "boom", duration_s: 1.2 },
      }),
    ]);
    expect(store.counters.failed).toBe(1);
    expect(store.counters.active).toBe(0);
    expect(store.tasks["t-abc1234"].data).toEqual({
      reason: "boom",
      duration_s: 1.2,
    });
  });

  it("counts in-flight tasks as active", () => {
    const store = run([
      event({ status: "started", seq: 0 }),
      event({ status: "working", seq: 1, data: { errors: 7 } }),
    ]);
    expect(store.counters.active).toBe(1);
    expect(store.counters.completed).toBe(0);
  });

  it("counts distinct task_ids as distinct tasks", () => {
    const store = run([
      event({ task_id: "t-a", agent_id: "a1", status: "complete", seq: 0 }),
      event({ task_id: "t-b", agent_id: "a2", status: "started", seq: 0 }),
      event({ task_id: "t-c", agent_id: "a3", status: "failed", seq: 0 }),
    ]);
    expect(store.counters).toEqual({
      total_tasks: 3,
      completed: 1,
      failed: 1,
      active: 1,
    });
  });

  it("returns the same store reference when the event is a duplicate", () => {
    const e = event({ status: "started", seq: 0 });
    const after = applyEvent(EMPTY_STORE, e);
    expect(applyEvent(after, e)).toBe(after);
  });

  it("returns a new store reference when an event is applied", () => {
    const e = event({ status: "started", seq: 0 });
    expect(applyEvent(EMPTY_STORE, e)).not.toBe(EMPTY_STORE);
  });
});

describe("applyEvent — activity feed", () => {
  it("pushes complete events onto the activity feed with current data", () => {
    const completeData = {
      metrics: { output_tokens: 487, decode_ms: 10579, tokens_per_sec: 46.0 },
    };
    const store = run([
      event({ status: "started", seq: 0 }),
      event({ status: "working", seq: 1, data: { message: "editing" } }),
      event({
        status: "complete",
        seq: 2,
        ts: "2026-05-27T14:03:18.000Z",
        data: completeData,
      }),
    ]);
    expect(store.activity).toEqual([
      {
        task_id: "t-abc1234",
        task_text: "Refactor extractor to use shared HTTP client",
        folder: "09-parallel-agents",
        agent_backend: "cc",
        ts: "2026-05-27T14:03:18.000Z",
        data: completeData,
      },
    ]);
  });

  it("orders entries newest-first across multiple completes", () => {
    const store = run([
      event({
        task_id: "t-abc",
        agent_id: "a1",
        status: "complete",
        seq: 0,
        ts: "2026-05-27T14:03:11.000Z",
        data: { duration_s: 1.0 },
      }),
      event({
        task_id: "t-def",
        agent_id: "a2",
        status: "complete",
        seq: 0,
        ts: "2026-05-27T14:03:12.000Z",
        data: { duration_s: 2.0 },
      }),
      event({
        task_id: "t-ghi",
        agent_id: "a3",
        status: "complete",
        seq: 0,
        ts: "2026-05-27T14:03:13.000Z",
        data: { duration_s: 3.0 },
      }),
    ]);
    expect(store.activity.map((e) => e.task_id)).toEqual([
      "t-ghi",
      "t-def",
      "t-abc",
    ]);
  });

  it(`caps the activity feed at ACTIVITY_FEED_LIMIT (${ACTIVITY_FEED_LIMIT})`, () => {
    const events: LifecycleEvent[] = [];
    for (let i = 0; i < ACTIVITY_FEED_LIMIT + 10; i += 1) {
      events.push(
        event({
          agent_id: `agent-${i}`,
          task_id: `t-${i}`,
          status: "complete",
          seq: 0,
          ts: `2026-05-27T14:03:${String(i).padStart(2, "0")}.000Z`,
          data: { duration_s: 1.0 },
        }),
      );
    }
    const store = events.reduce(applyEvent, EMPTY_STORE);
    expect(store.activity).toHaveLength(ACTIVITY_FEED_LIMIT);
    // Newest (last enqueued) should be at the front.
    expect(store.activity[0].task_id).toBe(`t-${ACTIVITY_FEED_LIMIT + 9}`);
  });

  it("does not push failed events onto the activity feed", () => {
    const store = run([
      event({ status: "started", seq: 0 }),
      event({
        status: "failed",
        seq: 1,
        data: { reason: "boom", duration_s: 1.2 },
      }),
    ]);
    expect(store.activity).toEqual([]);
  });
});

describe("applyManifest — upfront work-item announcement", () => {
  const manifest = {
    folder: "yt-dlp",
    agent_backend: "cc",
    tasks: [
      { task_id: "t-1", task_text: "a.py" },
      { task_id: "t-2", task_text: "b.py" },
      { task_id: "t-3", task_text: "c.py" },
    ],
  };

  it("seeds every announced task as pending, in manifest order", () => {
    const store = applyManifest(EMPTY_STORE, manifest);
    expect(Object.keys(store.tasks)).toEqual(["t-1", "t-2", "t-3"]);
    expect(
      Object.values(store.tasks).every((t) => t.status === "pending"),
    ).toBe(true);
    expect(store.counters).toEqual({
      total_tasks: 3,
      completed: 0,
      failed: 0,
      active: 0, // pending items have no agent yet
    });
  });

  it("registers the folder so the project list knows about it", () => {
    const store = applyManifest(EMPTY_STORE, manifest);
    expect(store.folders["yt-dlp"]).toEqual({
      first_started_ts: null,
      last_terminal_ts: null,
    });
  });

  it("never downgrades a task that already has live state", () => {
    const live = applyEvent(
      EMPTY_STORE,
      event({ task_id: "t-2", folder: "yt-dlp", status: "working" }),
    );
    const store = applyManifest(live, manifest);
    expect(store.tasks["t-2"].status).toBe("working");
    expect(store.tasks["t-1"].status).toBe("pending");
  });

  it("a later lifecycle event activates a pending task in place", () => {
    const seeded = applyManifest(EMPTY_STORE, manifest);
    const store = applyEvent(
      seeded,
      event({ task_id: "t-2", folder: "yt-dlp", status: "started" }),
    );
    expect(store.tasks["t-2"].status).toBe("started");
    // Position is unchanged: still second.
    expect(Object.keys(store.tasks)).toEqual(["t-1", "t-2", "t-3"]);
    expect(store.counters.active).toBe(1);
  });
});

describe("per-folder run clocks", () => {
  it("anchors on the earliest started and advances to the latest terminal", () => {
    const store = run([
      event({
        task_id: "t-1", agent_id: "a1", status: "started",
        ts: "2026-05-27T14:00:05.000Z",
      }),
      event({
        task_id: "t-2", agent_id: "a2", status: "started",
        ts: "2026-05-27T14:00:00.000Z", // earlier, arrives second
      }),
      event({
        task_id: "t-1", agent_id: "a1", seq: 1, status: "complete",
        ts: "2026-05-27T14:01:00.000Z",
      }),
      event({
        task_id: "t-2", agent_id: "a2", seq: 1, status: "failed",
        ts: "2026-05-27T14:00:30.000Z", // earlier terminal, arrives last
      }),
    ]);
    expect(store.folders["09-parallel-agents"]).toEqual({
      first_started_ts: "2026-05-27T14:00:00.000Z",
      last_terminal_ts: "2026-05-27T14:01:00.000Z",
    });
  });

  it("fromSnapshot carries the collector's folder clocks", () => {
    const snapshot: Snapshot = {
      first_started_ts: null,
      counters: { total_tasks: 0, completed: 0, failed: 0, active: 0 },
      tasks: {},
      folders: {
        "yt-dlp": {
          first_started_ts: "2026-05-27T14:00:00.000Z",
          last_terminal_ts: null,
        },
      },
    };
    expect(fromSnapshot(snapshot).folders).toEqual(snapshot.folders);
  });
});

describe("applyManifest — project display name", () => {
  it("records the project name on the folder entry", () => {
    const store = applyManifest(EMPTY_STORE, {
      folder: "01-unit-tests",
      agent_backend: "cc",
      project: "yt-dlp",
      tasks: [{ task_id: "t-1", task_text: "a.py" }],
    });
    expect(store.folders["01-unit-tests"].project).toBe("yt-dlp");
  });

  it("keeps an existing clock and project when re-announced without one", () => {
    const seeded = applyManifest(EMPTY_STORE, {
      folder: "f",
      agent_backend: "cc",
      project: "yt-dlp",
      tasks: [],
    });
    const after = applyManifest(seeded, {
      folder: "f",
      agent_backend: "cc",
      tasks: [],
    });
    expect(after.folders["f"].project).toBe("yt-dlp");
  });
});

describe("outputTokensPerSec — total fleet throughput across active agents", () => {
  function task(
    status: TaskState["status"],
    tokens_per_sec: number | null,
  ): TaskState {
    return {
      task_id: `t-${status}-${tokens_per_sec}`,
      task_text: "x",
      folder: "f",
      agent_backend: "cc",
      status,
      attempt: 0,
      data:
        tokens_per_sec == null
          ? {}
          : { metrics: { output_tokens: 100, decode_ms: 2000, tokens_per_sec } },
    };
  }

  it("returns null when no active agent is reporting metrics", () => {
    expect(outputTokensPerSec([])).toBeNull();
    expect(
      outputTokensPerSec([task("started", null), task("pending", null)]),
    ).toBeNull();
    // Terminal agents are not generating: their lifetime rate is excluded
    // (the App holds the last live reading for display continuity).
    expect(
      outputTokensPerSec([task("complete", 50), task("failed", 40)]),
    ).toBeNull();
  });

  it("sums tokens_per_sec over started/working agents", () => {
    const total = outputTokensPerSec([
      task("working", 40),
      task("started", 60),
      task("complete", 1000), // excluded — terminal, not generating
      task("pending", null), // excluded — no agent
      task("working", null), // excluded — no metrics yet
    ]);
    expect(total).toBe(100); // sum of 40 and 60
  });

  it("accepts a task record as well as an array", () => {
    const record = {
      a: task("working", 30),
      b: task("working", 70),
    };
    expect(outputTokensPerSec(record)).toBe(100);
  });
});
