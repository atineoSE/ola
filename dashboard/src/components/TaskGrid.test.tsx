import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TaskGrid } from "./TaskGrid";
import type { Status, TaskState, TaskStatus } from "../collector";

function task(
  task_id: string,
  status: TaskStatus,
  overrides: Partial<TaskState> = {},
): TaskState {
  return {
    task_id,
    task_text: `do ${task_id}`,
    folder: "09-parallel-agents",
    agent_backend: "cc",
    status,
    attempt: 0,
    data: {},
    ...overrides,
  };
}

afterEach(cleanup);

describe("<TaskGrid />", () => {
  it("renders the empty state when there are no tasks", () => {
    render(<TaskGrid tasks={[]} />);
    expect(screen.getByTestId("task-grid-empty")).toBeTruthy();
  });

  it("renders one cell per task", () => {
    render(
      <TaskGrid
        tasks={[
          task("t-a", "started"),
          task("t-b", "complete"),
          task("t-c", "failed"),
        ]}
      />,
    );
    const grid = screen.getByTestId("task-grid");
    expect(within(grid).getAllByRole("gridcell")).toHaveLength(3);
  });

  it("keeps cells in the given (manifest/dispatch) order — no reordering", () => {
    render(
      <TaskGrid
        tasks={[
          task("t-1", "started", { folder: "b", task_text: "alpha" }),
          task("t-2", "started", { folder: "a", task_text: "zeta" }),
          task("t-3", "started", { folder: "a", task_text: "alpha" }),
        ]}
      />,
    );
    const grid = screen.getByTestId("task-grid");
    const order = within(grid)
      .getAllByRole("gridcell")
      .map((c) => c.getAttribute("data-testid"));
    expect(order).toEqual(["task-cell-t-1", "task-cell-t-2", "task-cell-t-3"]);
  });

  it("renders pending tasks in the idle color (no pulse)", () => {
    render(<TaskGrid tasks={[task("t-a", "pending")]} />);
    const cell = screen.getByTestId("task-cell-t-a");
    expect(cell.getAttribute("data-status")).toBe("pending");
    expect(cell.className).toContain("bg-status-idle");
    expect(cell.className).not.toContain("animate-pulse");
  });

  it("renders complete tasks in the complete color (no pulse)", () => {
    render(<TaskGrid tasks={[task("t-a", "complete")]} />);
    const cell = screen.getByTestId("task-cell-t-a");
    expect(cell.getAttribute("data-status")).toBe("complete");
    expect(cell.className).toContain("bg-status-complete");
    expect(cell.className).not.toContain("animate-pulse");
  });

  it("renders failed tasks as unclaimed gray (back in the pool, no pulse)", () => {
    render(<TaskGrid tasks={[task("t-a", "failed")]} />);
    const cell = screen.getByTestId("task-cell-t-a");
    expect(cell.className).toContain("bg-status-idle");
    expect(cell.className).not.toContain("animate-pulse");
    // The hover tooltip keeps the why.
    expect(cell.getAttribute("title")).toContain("last attempt failed");
  });

  it.each<Status>(["started", "working"])(
    "renders %s tasks in the working color with a pulse",
    (status) => {
      render(<TaskGrid tasks={[task("t-a", status)]} />);
      const cell = screen.getByTestId("task-cell-t-a");
      expect(cell.className).toContain("bg-status-working");
      expect(cell.className).toContain("animate-pulse");
    },
  );

  it("tooltip shows task_text, friendly status, folder, and agent_backend", () => {
    render(
      <TaskGrid
        tasks={[
          task("t-a", "complete", {
            task_text: "Refactor extractor",
            folder: "09-parallel-agents",
            agent_backend: "oh",
          }),
        ]}
      />,
    );
    const title = screen.getByTestId("task-cell-t-a").getAttribute("title") ?? "";
    expect(title).toContain("Refactor extractor");
    expect(title).toContain("status: succeeded");
    expect(title).toContain("folder: 09-parallel-agents");
    expect(title).toContain("agent: oh");
  });

  it("tooltip surfaces throughput metrics from the metrics block when present", () => {
    render(
      <TaskGrid
        tasks={[
          task("t-a", "working", {
            data: {
              message: "editing",
              metrics: { output_tokens: 487, decode_ms: 10579, tokens_per_sec: 46.0 },
            },
          }),
        ]}
      />,
    );
    const title = screen.getByTestId("task-cell-t-a").getAttribute("title") ?? "";
    expect(title).toContain("tok/s: 46.0");
    expect(title).toContain("tokens: 487");
  });
});
