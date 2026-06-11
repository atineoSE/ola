/*
 * App-level tests for project selection: the title is a dropdown over the
 * folders the collector knows about, every panel is scoped to the picked
 * project, and with no data the title falls back to "no project available".
 */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { EMPTY_STORE, type CollectorStore, type TaskState } from "./collector";
import { useCollectorStream } from "./collector";

vi.mock("./collector", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./collector")>();
  return {
    ...actual,
    useCollectorStream: vi.fn(() => ({ store: actual.EMPTY_STORE, status: "open" })),
  };
});

const mockedStream = vi.mocked(useCollectorStream);

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

function storeWith(tasks: TaskState[]): CollectorStore {
  return {
    ...EMPTY_STORE,
    tasks: Object.fromEntries(tasks.map((t) => [t.task_id, t])),
    folders: Object.fromEntries(
      tasks.map((t) => [
        t.folder,
        { first_started_ts: null, last_terminal_ts: null },
      ]),
    ),
  };
}

afterEach(() => {
  cleanup();
  mockedStream.mockReset();
});

describe("<App /> project selection", () => {
  it("shows 'no project available' when the collector knows nothing", () => {
    mockedStream.mockReturnValue({ store: EMPTY_STORE, status: "open" });
    render(<App />);
    expect(screen.getByTestId("project-title-empty").textContent).toBe(
      "no project available",
    );
    expect(screen.queryByTestId("project-select")).toBeNull();
  });

  it("offers every known folder and defaults to the first, scoping the grid", () => {
    mockedStream.mockReturnValue({
      store: storeWith([
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
    mockedStream.mockReturnValue({
      store: storeWith([task("t-1", "alpha"), task("t-2", "beta")]),
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
  it("titles with the project (source folder) name, not the plan folder", () => {
    const store = storeWith([task("t-1", "01-unit-tests")]);
    store.folders["01-unit-tests"] = {
      first_started_ts: null,
      last_terminal_ts: null,
      project: "yt-dlp",
    };
    mockedStream.mockReturnValue({ store, status: "open" });
    render(<App />);

    const select = screen.getByTestId("project-select") as HTMLSelectElement;
    const option = within(select).getByRole("option") as HTMLOptionElement;
    expect(option.textContent).toBe("yt-dlp");
    // The underlying value stays the folder key (events group by it).
    expect(option.value).toBe("01-unit-tests");
  });
});
