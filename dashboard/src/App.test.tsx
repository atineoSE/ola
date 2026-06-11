/*
 * App-level tests for project selection: the title is a dropdown over the
 * folders the snapshot contains, every panel is scoped to the picked
 * project, and with no data the title falls back to "no project available".
 */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { EMPTY_SNAPSHOT, type Snapshot, type TaskState, useSnapshot } from "./snapshot";

vi.mock("./snapshot", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./snapshot")>();
  return {
    ...actual,
    useSnapshot: vi.fn(() => ({ snapshot: actual.EMPTY_SNAPSHOT, status: "open" })),
  };
});

// Same-origin concurrency fetch is irrelevant to these tests; stub it so the
// hook's mount effect doesn't hit a real network.
vi.stubGlobal(
  "fetch",
  vi.fn(() =>
    Promise.resolve(
      new Response(JSON.stringify({ concurrency: null }), { status: 200 }),
    ),
  ),
);

const mockedSnapshot = vi.mocked(useSnapshot);

function task(
  task_id: string,
  folder: string,
  overrides: Partial<TaskState> = {},
): TaskState {
  return {
    task_id,
    task_text: `do ${task_id}`,
    folder,
    agent_backend: "cc",
    status: "pending",
    attempt: 0,
    data: {},
    ...overrides,
  };
}

function snapshotWith(tasks: TaskState[]): Snapshot {
  return {
    ...EMPTY_SNAPSHOT,
    tasks: Object.fromEntries(tasks.map((t) => [t.task_id, t])),
    folders: Object.fromEntries(
      tasks.map((t) => [
        t.folder,
        { first_started_ts: null, last_terminal_ts: null, project: t.folder },
      ]),
    ),
  };
}

afterEach(() => {
  cleanup();
  mockedSnapshot.mockReset();
});

describe("<App /> project selection", () => {
  it("shows 'no project available' when the snapshot is empty", () => {
    mockedSnapshot.mockReturnValue({ snapshot: EMPTY_SNAPSHOT, status: "open" });
    render(<App />);
    expect(screen.getByTestId("project-title-empty").textContent).toBe(
      "no project available",
    );
    expect(screen.queryByTestId("project-select")).toBeNull();
  });

  it("offers every known folder and defaults to the first, scoping the grid", () => {
    mockedSnapshot.mockReturnValue({
      snapshot: snapshotWith([
        task("t-1", "yt-dlp"),
        task("t-2", "yt-dlp"),
        task("t-3", "other-project"),
      ]),
      status: "open",
    });
    render(<App />);

    const select = screen.getByTestId("project-select") as HTMLSelectElement;
    const labels = within(select)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(labels).toEqual(["other-project", "yt-dlp"]); // sorted
    expect(select.value).toBe("other-project");

    // Only the selected project's items are on the grid.
    expect(screen.getByTestId("task-cell-t-3")).toBeTruthy();
    expect(screen.queryByTestId("task-cell-t-1")).toBeNull();
  });

  it("switches every panel when another project is picked", () => {
    mockedSnapshot.mockReturnValue({
      snapshot: snapshotWith([task("t-1", "alpha"), task("t-2", "beta")]),
      status: "open",
    });
    render(<App />);

    const select = screen.getByTestId("project-select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "beta" } });

    expect(screen.getByTestId("task-cell-t-2")).toBeTruthy();
    expect(screen.queryByTestId("task-cell-t-1")).toBeNull();
  });
});

describe("<App /> project display names", () => {
  it("titles with the folder's display name", () => {
    const snapshot = snapshotWith([task("t-1", "01-unit-tests")]);
    snapshot.folders["01-unit-tests"] = {
      first_started_ts: null,
      last_terminal_ts: null,
      project: "01-unit-tests",
    };
    mockedSnapshot.mockReturnValue({ snapshot, status: "open" });
    render(<App />);

    const select = screen.getByTestId("project-select") as HTMLSelectElement;
    const option = within(select).getByRole("option") as HTMLOptionElement;
    expect(option.textContent).toBe("01-unit-tests");
    expect(option.value).toBe("01-unit-tests");
  });
});
