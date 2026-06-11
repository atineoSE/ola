import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MetricSample, TaskState } from "../snapshot";
import { useOutputTokensPerSec } from "./useOutputTokensPerSec";

function task(id: string, sample?: MetricSample): TaskState {
  return {
    task_id: id,
    task_text: "x",
    folder: "f",
    agent_backend: "cc",
    status: "working",
    attempt: 0,
    data:
      sample == null
        ? {}
        : { metrics: { ...sample, tokens_per_sec: 0 } },
  };
}

describe("useOutputTokensPerSec", () => {
  it("is all-null on the first poll (no prior sample yet)", () => {
    const { result } = renderHook(
      ({ tasks }) => useOutputTokensPerSec(tasks, "f"),
      {
        initialProps: {
          tasks: [task("t-a", { output_tokens: 100, decode_ms: 2000 })],
        },
      },
    );
    expect(result.current).toEqual({ current: null, avg: null, max: null });
  });

  it("reports the windowed rate once a second poll lands", () => {
    const { result, rerender } = renderHook(
      ({ tasks }) => useOutputTokensPerSec(tasks, "f"),
      {
        initialProps: {
          tasks: [task("t-a", { output_tokens: 100, decode_ms: 2000 })],
        },
      },
    );
    // +100 tokens over +1000ms decode → 100 tok/s.
    rerender({ tasks: [task("t-a", { output_tokens: 200, decode_ms: 3000 })] });
    expect(result.current.current).toBe(100);
  });

  it("tracks the running average and peak across readings", () => {
    const { result, rerender } = renderHook(
      ({ tasks }) => useOutputTokensPerSec(tasks, "f"),
      {
        initialProps: {
          tasks: [task("t-a", { output_tokens: 0, decode_ms: 0 })],
        },
      },
    );
    // +100 over +1000ms → 100 tok/s.
    rerender({ tasks: [task("t-a", { output_tokens: 100, decode_ms: 1000 })] });
    expect(result.current).toEqual({ current: 100, avg: 100, max: 100 });
    // +200 over +1000ms → 200 tok/s; avg (100+200)/2 = 150, peak 200.
    rerender({ tasks: [task("t-a", { output_tokens: 300, decode_ms: 2000 })] });
    expect(result.current).toEqual({ current: 200, avg: 150, max: 200 });
    // +50 over +1000ms → 50 tok/s; avg (100+200+50)/3 ≈ 116.67, peak stays 200.
    rerender({ tasks: [task("t-a", { output_tokens: 350, decode_ms: 3000 })] });
    expect(result.current.current).toBe(50);
    expect(result.current.avg).toBeCloseTo(116.67, 1);
    expect(result.current.max).toBe(200);
  });

  it("holds the last current reading across a no-progress gap", () => {
    const { result, rerender } = renderHook(
      ({ tasks }) => useOutputTokensPerSec(tasks, "f"),
      {
        initialProps: {
          tasks: [task("t-a", { output_tokens: 100, decode_ms: 2000 })],
        },
      },
    );
    rerender({ tasks: [task("t-a", { output_tokens: 200, decode_ms: 3000 })] });
    expect(result.current.current).toBe(100);
    // Same counters next poll → no advance → hold the last value, not null.
    rerender({ tasks: [task("t-a", { output_tokens: 200, decode_ms: 3000 })] });
    expect(result.current.current).toBe(100);
  });

  it("resets to all-null on a project switch", () => {
    const { result, rerender } = renderHook(
      ({ tasks, project }) => useOutputTokensPerSec(tasks, project),
      {
        initialProps: {
          tasks: [task("t-a", { output_tokens: 100, decode_ms: 2000 })],
          project: "f",
        },
      },
    );
    rerender({
      tasks: [task("t-a", { output_tokens: 200, decode_ms: 3000 })],
      project: "f",
    });
    expect(result.current.current).toBe(100);
    // Switching folders drops the held reading and the carried samples.
    rerender({
      tasks: [task("t-a", { output_tokens: 999, decode_ms: 9000 })],
      project: "g",
    });
    expect(result.current).toEqual({ current: null, avg: null, max: null });
  });
});
