import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MetricSample, TaskState } from "../snapshot";
import { useLiveTokensPerSec } from "./useLiveTokensPerSec";

function task(
  id: string,
  sample?: MetricSample,
  status: TaskState["status"] = "working",
): TaskState {
  return {
    task_id: id,
    task_text: "x",
    folder: "f",
    agent_backend: "cc",
    status,
    attempt: 0,
    data: sample == null ? {} : { metrics: { ...sample, tokens_per_sec: 0 } },
  };
}

describe("useLiveTokensPerSec", () => {
  it("is null on the first poll (no prior sample yet)", () => {
    const { result } = renderHook(
      ({ tasks }) => useLiveTokensPerSec(tasks, "f"),
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
      ({ tasks }) => useLiveTokensPerSec(tasks, "f"),
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

  it("holds the last reading across a no-progress gap while still active", () => {
    const { result, rerender } = renderHook(
      ({ tasks }) => useLiveTokensPerSec(tasks, "f"),
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

  it("drops to null once no agent is active (run finished)", () => {
    const { result, rerender } = renderHook(
      ({ tasks }) => useLiveTokensPerSec(tasks, "f"),
      {
        initialProps: {
          tasks: [task("t-a", { output_tokens: 100, decode_ms: 2000 })],
        },
      },
    );
    rerender({ tasks: [task("t-a", { output_tokens: 200, decode_ms: 3000 })] });
    expect(result.current).toBe(100);
    // The task reaches a terminal status: no live decode → the tile reads "—".
    rerender({
      tasks: [task("t-a", { output_tokens: 200, decode_ms: 3000 }, "complete")],
    });
    expect(result.current).toBeNull();
  });

  it("stays null when every task is already terminal (fresh load of a finished run)", () => {
    const { result } = renderHook(
      ({ tasks }) => useLiveTokensPerSec(tasks, "f"),
      {
        initialProps: {
          tasks: [
            task("t-a", { output_tokens: 500, decode_ms: 5000 }, "complete"),
            task("t-b", { output_tokens: 300, decode_ms: 4000 }, "failed"),
          ],
        },
      },
    );
    expect(result.current).toBeNull();
  });

  it("resets to null on a project switch", () => {
    const { result, rerender } = renderHook(
      ({ tasks, project }) => useLiveTokensPerSec(tasks, project),
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
    // Switching folders drops the held reading and the carried samples, so the
    // new project's first poll is back to null (needs two of its own samples).
    rerender({
      tasks: [task("t-a", { output_tokens: 999, decode_ms: 9000 })],
      project: "g",
    });
    expect(result.current).toBeNull();
  });
});
