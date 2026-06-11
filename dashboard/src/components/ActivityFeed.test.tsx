import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ActivityFeed } from "./ActivityFeed";
import type { ActivityEntry } from "../collector";

function entry(
  task_id: string,
  overrides: Partial<ActivityEntry> = {},
): ActivityEntry {
  return {
    task_id,
    task_text: `do ${task_id}`,
    folder: "09-parallel-agents",
    agent_backend: "cc",
    ts: "2026-05-27T14:03:11.000Z",
    data: { metrics: { output_tokens: 487, decode_ms: 10579, tokens_per_sec: 46.0 } },
    ...overrides,
  };
}

afterEach(cleanup);

describe("<ActivityFeed />", () => {
  it("renders the empty state when there are no entries", () => {
    render(<ActivityFeed entries={[]} />);
    expect(screen.getByTestId("activity-feed-empty")).toBeTruthy();
    expect(screen.queryByTestId("activity-feed-list")).toBeNull();
  });

  it("renders one row per entry in the given order (newest-first)", () => {
    render(
      <ActivityFeed
        entries={[entry("t-ghi"), entry("t-def"), entry("t-abc")]}
      />,
    );
    const list = screen.getByTestId("activity-feed-list");
    const items = within(list).getAllByRole("listitem");
    expect(items.map((li) => li.getAttribute("data-testid"))).toEqual([
      "activity-row-t-ghi",
      "activity-row-t-def",
      "activity-row-t-abc",
    ]);
  });

  it("renders the free-text task_text as the row label", () => {
    render(
      <ActivityFeed
        entries={[entry("t-a", { task_text: "Refactor extractor" })]}
      />,
    );
    expect(screen.getByText("Refactor extractor")).toBeTruthy();
  });

  it("surfaces agent_backend and folder from the envelope", () => {
    render(
      <ActivityFeed
        entries={[entry("t-a", { agent_backend: "oh", folder: "07-foo" })]}
      />,
    );
    expect(screen.getByText(/oh · 07-foo/)).toBeTruthy();
  });

  it("shows the throughput badge from the metrics block", () => {
    render(
      <ActivityFeed
        entries={[
          entry("t-a", {
            data: { metrics: { output_tokens: 500, decode_ms: 10000, tokens_per_sec: 12.345 } },
          }),
        ]}
      />,
    );
    expect(screen.getByText("12.3 tok/s")).toBeTruthy();
  });

  it("omits the throughput badge when there is no metrics block", () => {
    render(<ActivityFeed entries={[entry("t-a", { data: {} })]} />);
    expect(screen.queryByText(/tok\/s$/)).toBeNull();
  });

  it("appends total output tokens to the context line", () => {
    render(
      <ActivityFeed
        entries={[
          entry("t-a", {
            agent_backend: "cc",
            folder: "09-parallel-agents",
            data: { metrics: { output_tokens: 1234, decode_ms: 26000, tokens_per_sec: 47.5 } },
          }),
        ]}
      />,
    );
    expect(
      screen.getByText(/cc · 09-parallel-agents · 1,234 tok/),
    ).toBeTruthy();
  });

  it("shows just backend · folder when data carries no metrics", () => {
    render(
      <ActivityFeed
        entries={[
          entry("t-a", {
            agent_backend: "cc",
            folder: "09-parallel-agents",
            data: {},
          }),
        ]}
      />,
    );
    expect(screen.getByText("cc · 09-parallel-agents")).toBeTruthy();
  });
});
