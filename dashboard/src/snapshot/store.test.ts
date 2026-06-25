import { describe, expect, it } from "vitest";

import {
  meanTaskTokensPerSec,
  recomputeCounters,
  sumOutputTokens,
  windowedTokensPerSec,
} from "./store";
import type { MetricSample } from "./store";
import type { TaskState, TaskStatus } from "./types";

function task(
  status: TaskStatus,
  sample?: MetricSample,
  id = `t-${status}`,
): TaskState {
  return {
    task_id: id,
    task_text: "x",
    folder: "f",
    agent_backend: "cc",
    status,
    attempt: 0,
    data:
      sample == null
        ? {}
        : {
            metrics: {
              output_tokens: sample.output_tokens,
              decode_ms: sample.decode_ms,
              // tokens_per_sec must be a finite number for readMetrics to
              // accept the block, but the windowed rate ignores its value.
              tokens_per_sec: 0,
            },
          },
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

describe("sumOutputTokens", () => {
  it("sums output_tokens across every task carrying metrics", () => {
    const total = sumOutputTokens(
      record([
        task("complete", { output_tokens: 500, decode_ms: 1000 }),
        task("working", { output_tokens: 250, decode_ms: 800 }),
        task("started", { output_tokens: 50, decode_ms: 100 }),
      ]),
    );
    expect(total).toBe(800);
  });

  it("ignores tasks with no metrics block (pending / no usage reported)", () => {
    const total = sumOutputTokens(
      record([
        task("complete", { output_tokens: 1000, decode_ms: 2000 }),
        task("pending"),
      ]),
    );
    expect(total).toBe(1000);
  });

  it("is 0 for an empty run", () => {
    expect(sumOutputTokens([])).toBe(0);
  });
});

describe("meanTaskTokensPerSec — decode-weighted per-task average", () => {
  it("is Σ tokens / Σ decode-seconds (a weighted mean, between the rates)", () => {
    // 100 tok / 2s = 50/s and 300 tok / 2s = 150/s; weighted 400 / 4s = 100/s,
    // which sits between the two per-task rates.
    const avg = meanTaskTokensPerSec([
      task("complete", { output_tokens: 100, decode_ms: 2000 }),
      task("complete", { output_tokens: 300, decode_ms: 2000 }, "t-b"),
    ]);
    expect(avg).toBe(100);
  });

  it("never exceeds the peak per-task rate (the invariant the tile relies on)", () => {
    // Fast task 200/1s = 200; slow task 100/10s = 10. Weighted average
    // 300 / 11s ≈ 27.3 — comfortably ≤ the 200/s peak.
    const avg = meanTaskTokensPerSec([
      task("complete", { output_tokens: 200, decode_ms: 1000 }),
      task("complete", { output_tokens: 100, decode_ms: 10000 }, "t-b"),
    ])!;
    expect(avg).toBeCloseTo(27.27, 1);
    expect(avg).toBeLessThanOrEqual(200);
  });

  it("skips tasks with no metrics or a non-positive decode time", () => {
    const avg = meanTaskTokensPerSec([
      task("complete", { output_tokens: 100, decode_ms: 2000 }),
      task("pending"),
      task("complete", { output_tokens: 999, decode_ms: 0 }, "t-z"),
    ]);
    expect(avg).toBe(50); // only the first task counts: 100 / 2s
  });

  it("is null when no task carries usable metrics", () => {
    expect(meanTaskTokensPerSec([])).toBeNull();
    expect(meanTaskTokensPerSec([task("pending")])).toBeNull();
  });
});

describe("windowedTokensPerSec — fleet throughput as a Δ window", () => {
  it("returns null and empty samples when no active agent reports metrics", () => {
    expect(windowedTokensPerSec({}, [])).toEqual({ value: null, samples: {} });
    const r = windowedTokensPerSec({}, [
      task("started"),
      task("pending"),
      task("complete", { output_tokens: 100, decode_ms: 2000 }),
    ]);
    // Terminal/pending/no-metrics agents are not generating now.
    expect(r.value).toBeNull();
    expect(r.samples).toEqual({});
  });

  it("returns null on first sighting but carries the sample forward", () => {
    const r = windowedTokensPerSec({}, [
      task("working", { output_tokens: 100, decode_ms: 2000 }, "t-a"),
    ]);
    expect(r.value).toBeNull(); // need two points for a rate
    expect(r.samples).toEqual({ "t-a": { output_tokens: 100, decode_ms: 2000 } });
  });

  it("computes Δtokens / Δdecode_ms once a prior sample exists", () => {
    // +100 tokens over +1000ms decode → 100 tok/s.
    const r = windowedTokensPerSec(
      { "t-a": { output_tokens: 100, decode_ms: 2000 } },
      [task("working", { output_tokens: 200, decode_ms: 3000 }, "t-a")],
    );
    expect(r.value).toBe(100);
  });

  it("sums per-agent windowed rates across the fleet", () => {
    const r = windowedTokensPerSec(
      {
        "t-a": { output_tokens: 100, decode_ms: 2000 },
        "t-b": { output_tokens: 0, decode_ms: 0 },
      },
      [
        task("working", { output_tokens: 200, decode_ms: 3000 }, "t-a"), // +100/1s = 100
        task("started", { output_tokens: 50, decode_ms: 1000 }, "t-b"), // +50/1s = 50
      ],
    );
    expect(r.value).toBe(150);
  });

  it("ignores no-progress (Δdecode 0) and counter resets (negative Δ)", () => {
    const noProgress = windowedTokensPerSec(
      { "t-a": { output_tokens: 100, decode_ms: 2000 } },
      [task("working", { output_tokens: 100, decode_ms: 2000 }, "t-a")],
    );
    expect(noProgress.value).toBeNull();
    // sample still refreshed for the next window
    expect(noProgress.samples["t-a"]).toEqual({
      output_tokens: 100,
      decode_ms: 2000,
    });

    const reset = windowedTokensPerSec(
      { "t-a": { output_tokens: 500, decode_ms: 9000 } },
      [task("working", { output_tokens: 20, decode_ms: 400 }, "t-a")],
    );
    expect(reset.value).toBeNull();
  });

  it("accepts a task record as well as an array", () => {
    const r = windowedTokensPerSec(
      { "t-a": { output_tokens: 100, decode_ms: 2000 } },
      record([task("working", { output_tokens: 300, decode_ms: 4000 }, "t-a")]),
    );
    // +200 tokens / +2000ms = 100 tok/s
    expect(r.value).toBe(100);
  });
});
