import { describe, expect, it } from "vitest";

import { outputTokensPerSec, recomputeCounters } from "./store";
import type { TaskState } from "./types";

function task(
  status: TaskState["status"],
  tokens_per_sec: number | null = null,
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

function record(tasks: TaskState[]): Record<string, TaskState> {
  return Object.fromEntries(tasks.map((t, i) => [`${t.task_id}-${i}`, t]));
}

describe("recomputeCounters", () => {
  it("counts complete / failed / active, with pending excluded from active", () => {
    const counters = recomputeCounters(
      record([
        task("complete"),
        task("failed"),
        task("started"),
        task("working"),
        task("pending"),
      ]),
    );
    expect(counters).toEqual({
      total_tasks: 5,
      completed: 1,
      failed: 1,
      active: 2, // started + working; pending has no agent yet
    });
  });

  it("is empty for no tasks", () => {
    expect(recomputeCounters({})).toEqual({
      total_tasks: 0,
      completed: 0,
      failed: 0,
      active: 0,
    });
  });
});

describe("outputTokensPerSec — total fleet throughput across active agents", () => {
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
    expect(
      outputTokensPerSec(record([task("working", 30), task("working", 70)])),
    ).toBe(100);
  });
});
