import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MetricsPanel } from "./MetricsPanel";
import type { TaskState } from "../snapshot";

function task(
  task_id: string,
  data: Record<string, unknown>,
  overrides: Partial<TaskState> = {},
): TaskState {
  return {
    task_id,
    task_text: `do ${task_id}`,
    folder: "09-parallel-agents",
    agent_backend: "cc",
    status: "working",
    attempt: 0,
    data,
    ...overrides,
  };
}

/** A `data` payload carrying a metrics block. */
function metrics(
  output_tokens: number,
  decode_ms: number,
  tokens_per_sec: number,
): Record<string, unknown> {
  return { metrics: { output_tokens, decode_ms, tokens_per_sec } };
}

afterEach(cleanup);

describe("<MetricsPanel />", () => {
  it("renders the empty state when no task has metrics", () => {
    render(<MetricsPanel tasks={[]} />);
    expect(screen.getByTestId("metrics-panel-empty")).toBeTruthy();
    expect(screen.queryByTestId("metrics-panel-list")).toBeNull();
  });

  it("omits tasks whose data has no metrics block", () => {
    render(
      <MetricsPanel
        tasks={[
          task("t-a", {}),
          task("t-b", { message: "running tests" }),
        ]}
      />,
    );
    // Neither task carries a metrics block → nothing to show.
    expect(screen.getByTestId("metrics-panel-empty")).toBeTruthy();
  });

  it("renders one row per task that has metrics", () => {
    render(
      <MetricsPanel
        tasks={[
          task("t-a", metrics(120, 2600, 46.2)),
          task("t-b", metrics(80, 1800, 44.4)),
          task("t-c", {}),
        ]}
      />,
    );
    const list = screen.getByTestId("metrics-panel-list");
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((li) => li.getAttribute("data-testid"))).toEqual([
      "metrics-row-t-a",
      "metrics-row-t-b",
    ]);
  });

  it("renders a chip for throughput and volume from the metrics block", () => {
    render(<MetricsPanel tasks={[task("t-a", metrics(487, 10579, 46.0))]} />);
    expect(screen.getByTestId("metric-chip-t-a-tok/s").textContent).toBe(
      "tok/s: 46.0",
    );
    expect(screen.getByTestId("metric-chip-t-a-tokens").textContent).toBe(
      "tokens: 487",
    );
  });

  it("shows the task_text and agent_backend on each row", () => {
    render(
      <MetricsPanel
        tasks={[
          task("t-a", metrics(80, 1800, 44.4), {
            task_text: "Refactor extractor",
            agent_backend: "oh",
          }),
        ]}
      />,
    );
    const row = screen.getByTestId("metrics-row-t-a");
    expect(within(row).getByText("Refactor extractor")).toBeTruthy();
    expect(within(row).getByText("oh")).toBeTruthy();
  });
});
