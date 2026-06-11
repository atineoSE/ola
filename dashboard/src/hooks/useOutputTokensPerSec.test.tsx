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
  it("is null on the first poll (no prior sample yet)", () => {
    const { result } = renderHook(
      ({ tasks }) => useOutputTokensPerSec(tasks, "f"),
      {
        initialProps: {
          tasks: [task("t-a", { output_tokens: 100, decode_ms: 2000 })],
        },
      },
    );
    expect(result.current).toBeNull();
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
    expect(result.current).toBe(100);
  });

  it("holds the last reading across a no-progress gap", () => {
    const { result, rerender } = renderHook(
      ({ tasks }) => useOutputTokensPerSec(tasks, "f"),
      {
        initialProps: {
          tasks: [task("t-a", { output_tokens: 100, decode_ms: 2000 })],
        },
      },
    );
    rerender({ tasks: [task("t-a", { output_tokens: 200, decode_ms: 3000 })] });
    expect(result.current).toBe(100);
    // Same counters next poll → no advance → hold the last value, not null.
    rerender({ tasks: [task("t-a", { output_tokens: 200, decode_ms: 3000 })] });
    expect(result.current).toBe(100);
  });

  it("resets to null on a project switch", () => {
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
    expect(result.current).toBe(100);
    // Switching folders drops the held reading and the carried samples.
    rerender({
      tasks: [task("t-a", { output_tokens: 999, decode_ms: 9000 })],
      project: "g",
    });
    expect(result.current).toBeNull();
  });
});
