import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TaskGrid } from "./TaskGrid";
import { __cellRenderCount } from "./TaskGrid.instrumentation";
import type { TaskState } from "../snapshot";

afterEach(cleanup);

/**
 * Large-grid smoke test. Each Cell is wrapped in React.memo so a single
 * arriving event should only re-execute one cell's render function — not
 * the whole grid.
 *
 * `__cellRenderCount` is a test-only counter bumped inside Cell's body;
 * memo bailouts skip the body, so it gives us a direct render count.
 */

function makeTasks(n: number): TaskState[] {
  const folders = ["common", "extractor", "downloader", "postprocessor"];
  const tasks: TaskState[] = [];
  for (let i = 0; i < n; i++) {
    tasks.push({
      task_id: `t-${i.toString().padStart(5, "0")}`,
      task_text: `task ${i}`,
      folder: folders[i % folders.length],
      agent_backend: "cc",
      status: "started",
      attempt: 0,
      data: {},
    });
  }
  return tasks;
}

beforeEach(() => {
  if (__cellRenderCount) __cellRenderCount.count = 0;
});

describe("<TaskGrid /> at scale", () => {
  it("exposes the test-only render counter when running under vitest", () => {
    expect(__cellRenderCount).not.toBeNull();
  });

  it("mounts 2,000 cells without dropping any", () => {
    const tasks = makeTasks(2000);
    const { container } = render(<TaskGrid tasks={tasks} />);
    const cells = container.querySelectorAll('[role="gridcell"]');
    expect(cells.length).toBe(2000);
    // Mount should render every cell exactly once.
    expect(__cellRenderCount!.count).toBe(2000);
  });

  it("a single-task update re-renders only that one cell", () => {
    const tasks = makeTasks(2000);
    const { rerender, getByTestId } = render(<TaskGrid tasks={tasks} />);

    expect(__cellRenderCount!.count).toBe(2000);
    __cellRenderCount!.count = 0;

    // Flip one task to `working`, mirroring how applyEvent yields a fresh
    // tasks map with one changed entry and the rest referentially stable.
    const next = tasks.slice();
    next[1000] = { ...next[1000], status: "working" };

    rerender(<TaskGrid tasks={next} />);

    expect(getByTestId("task-cell-t-01000").getAttribute("data-status")).toBe(
      "working",
    );
    // The headline assertion: only the touched cell's render body ran.
    expect(__cellRenderCount!.count).toBe(1);
  });

  it("two unrelated updates each re-render only their own cell", () => {
    const tasks = makeTasks(2000);
    const { rerender } = render(<TaskGrid tasks={tasks} />);
    __cellRenderCount!.count = 0;

    const afterA = tasks.slice();
    afterA[500] = { ...afterA[500], status: "complete" };
    rerender(<TaskGrid tasks={afterA} />);
    expect(__cellRenderCount!.count).toBe(1);

    const afterB = afterA.slice();
    afterB[1500] = { ...afterB[1500], status: "working" };
    rerender(<TaskGrid tasks={afterB} />);

    // Only index 1500 changed; index 500's props are identical, memo bails.
    expect(__cellRenderCount!.count).toBe(2);
  });
});
