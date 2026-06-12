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

  it("offers every known folder sorted, defaulting to the frontier run", () => {
    // Nothing running and nothing finished here (all pending), so the default
    // is the last folder in run order with outstanding work — "yt-dlp".
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
    expect(select.value).toBe("yt-dlp");

    // Only the selected project's items are on the grid.
    expect(screen.getByTestId("task-cell-t-1")).toBeTruthy();
    expect(screen.queryByTestId("task-cell-t-3")).toBeNull();
  });

  it("defaults to the folder with an agent currently running", () => {
    // 00 finished, 02 a future run, 01 active → the active run wins regardless
    // of run order.
    const snapshot = snapshotWith([
      task("t-0", "00-done", { status: "complete" }),
      task("t-1", "01-live", { status: "working" }),
      task("t-2", "02-next"),
    ]);
    snapshot.folders["01-live"] = {
      first_started_ts: null,
      last_terminal_ts: null,
      project: "01-live",
      active_anchor_ts: "2026-05-27T14:03:15.000Z",
    };
    mockedSnapshot.mockReturnValue({ snapshot, status: "open" });
    render(<App />);

    const select = screen.getByTestId("project-select") as HTMLSelectElement;
    expect(select.value).toBe("01-live");
  });

  it("defaults to the last unfinished folder when nothing is running", () => {
    // 00 + 01 finished, 02 still has work and no agent is active → 02.
    mockedSnapshot.mockReturnValue({
      snapshot: snapshotWith([
        task("t-0", "00-done", { status: "complete" }),
        task("t-1", "01-done", { status: "complete" }),
        task("t-2", "02-next"),
      ]),
      status: "open",
    });
    render(<App />);

    const select = screen.getByTestId("project-select") as HTMLSelectElement;
    expect(select.value).toBe("02-next");
  });

  it("defaults to the last folder when every run is finished", () => {
    mockedSnapshot.mockReturnValue({
      snapshot: snapshotWith([
        task("t-0", "00-done", { status: "complete" }),
        task("t-1", "01-done", { status: "complete" }),
      ]),
      status: "open",
    });
    render(<App />);

    const select = screen.getByTestId("project-select") as HTMLSelectElement;
    expect(select.value).toBe("01-done");
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

describe("<App /> agent identity and theme", () => {
  it("names the agent and model and themes the accent to the backend color", () => {
    const snapshot = snapshotWith([task("t-1", "09-par", { agent_backend: "cc" })]);
    snapshot.folders["09-par"] = {
      first_started_ts: null,
      last_terminal_ts: null,
      project: "09-par",
      agent_backend: "cc",
      models: ["claude-opus-4-8", "claude-haiku-4-5"],
    };
    mockedSnapshot.mockReturnValue({ snapshot, status: "open" });
    const { container } = render(<App />);

    expect(screen.getByTestId("agent-name").textContent).toBe("Claude Code");
    expect(screen.getByTestId("agent-model").textContent).toBe(
      "claude-opus-4-8, claude-haiku-4-5",
    );
    // The agent's signature color fills the page background and stays the
    // in-panel accent; the header switches to a contrasting ink.
    const main = container.querySelector("main") as HTMLElement;
    expect(main.style.getPropertyValue("--color-accent")).toBe("#CB7153");
    expect(main.style.background).toBe("rgb(203, 113, 83)"); // #CB7153
    const header = container.querySelector("header") as HTMLElement;
    expect(header.style.color).toBe("rgb(245, 247, 250)"); // light ink on terracotta
  });

  it("keeps the default dark page (no themed background) before a backend lands", () => {
    const snapshot = snapshotWith([task("t-1", "09-par", { agent_backend: "" })]);
    snapshot.folders["09-par"] = {
      first_started_ts: null,
      last_terminal_ts: null,
      project: "09-par",
    };
    mockedSnapshot.mockReturnValue({ snapshot, status: "open" });
    const { container } = render(<App />);
    const main = container.querySelector("main") as HTMLElement;
    expect(main.style.background).toBe("");
    const header = container.querySelector("header") as HTMLElement;
    expect(header.style.color).toBe("");
  });

  it("omits the agent identity until a backend is known", () => {
    const snapshot = snapshotWith([task("t-1", "09-par")]);
    snapshot.folders["09-par"] = {
      first_started_ts: null,
      last_terminal_ts: null,
      project: "09-par",
    };
    mockedSnapshot.mockReturnValue({ snapshot, status: "open" });
    render(<App />);
    expect(screen.queryByTestId("agent-identity")).toBeNull();
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
