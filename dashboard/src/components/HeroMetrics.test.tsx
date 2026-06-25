import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HeroMetrics } from "./HeroMetrics";
import type { Counters, ProgressMetric } from "../snapshot";

function counters(overrides: Partial<Counters> = {}): Counters {
  return {
    total_tasks: 0,
    completed: 0,
    failed: 0,
    active: 0,
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-05-27T14:03:25.000Z"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("<HeroMetrics />", () => {
  it("renders the task lifecycle tiles from the store counters", () => {
    render(
      <HeroMetrics
        counters={counters({
          total_tasks: 1045,
          completed: 12,
          failed: 1,
          active: 4,
        })}      />,
    );
    // Failed attempts return their task to the pool — not finished work.
    expect(screen.getByTestId("tasks-completed-value").textContent).toBe(
      "12 / 1,045",
    );
    expect(screen.getByTestId("active-agents-value").textContent).toBe("4");
    expect(screen.getByTestId("failed-value").textContent).toBe("1");
  });

  it("shows --:-- for elapsed time before any agent has run", () => {
    render(<HeroMetrics counters={counters({ total_tasks: 5 })} />);
    expect(screen.getByTestId("elapsed-value").textContent).toBe("--:--");
  });

  it("freezes elapsed at the accumulated active seconds when idle", () => {
    // No anchor → run is idle; the readout holds the accumulated base and
    // ignores wall-clock time passing.
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 5 })}
        activeElapsedSeconds={75}
        activeAnchorTs={null}
      />,
    );
    expect(screen.getByTestId("elapsed-value").textContent).toBe("01:15");
  });

  it("ticks the active tail from the anchor on top of the base", () => {
    // Base 5s accumulated, last event (anchor) 10s ago → 00:15 and counting.
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 5, active: 1 })}
        activeElapsedSeconds={5}
        activeAnchorTs="2026-05-27T14:03:15.000Z"
      />,
    );
    expect(screen.getByTestId("elapsed-value").textContent).toBe("00:15");
  });

  it("sets progress-bar width to the completed fraction of total_tasks", () => {
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 1000, completed: 240, failed: 10 })}      />,
    );
    // failed: 10 is excluded — those tasks went back to the pool.
    const fill = screen.getByTestId("progress-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("24%");
    const bar = screen.getByRole("progressbar");
    expect(bar.getAttribute("aria-valuenow")).toBe("24");
  });

  it("does not divide by zero when total_tasks is 0", () => {
    render(<HeroMetrics counters={counters()} />);
    const fill = screen.getByTestId("progress-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("0%");
  });

  it("clamps a runaway progress fraction at 100%", () => {
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 10, completed: 12, failed: 0 })}      />,
    );
    const fill = screen.getByTestId("progress-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("100%");
  });
});

describe("<HeroMetrics /> folded tiles", () => {
  it("shows the failed count beneath the tasks total, muted when zero", () => {
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 10, completed: 4, failed: 0 })}
      />,
    );
    const failed = screen.getByTestId("failed-value");
    expect(failed.textContent).toBe("0");
    // A clean run keeps the failed figure muted, not alarmed.
    expect(failed.className).toContain("text-text-muted");
    expect(screen.getByText("failed")).toBeTruthy();
  });

  it("colours the failed count when non-zero", () => {
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 10, completed: 4, failed: 3 })}
      />,
    );
    const failed = screen.getByTestId("failed-value");
    expect(failed.textContent).toBe("3");
    expect(failed.className).toContain("text-status-failed");
  });

  it("folds the output-token total beneath the elapsed clock", () => {
    render(
      <HeroMetrics counters={counters()} totalOutputTokens={2_500_000} />,
    );
    expect(screen.getByTestId("elapsed-value")).toBeTruthy();
    expect(screen.getByTestId("total-output-tokens-value").textContent).toBe(
      "2.50",
    );
    expect(screen.getByText(/M output tokens/)).toBeTruthy();
  });
});

describe("<HeroMetrics /> agents stepper", () => {
  const counters = {
    total_tasks: 10,
    completed: 2,
    failed: 0,
    active: 4,
  };

  it("shows actual / target with labels when the control is wired", () => {
    const onChange = vi.fn();
    render(
      <HeroMetrics
        counters={counters}        agentsTarget={20}
        onAgentsTargetChange={onChange}
      />,
    );
    expect(screen.getByTestId("active-agents-value").textContent).toBe("4");
    expect(screen.getByTestId("agents-target-value").textContent).toBe("20");
    expect(screen.getByText("Actual")).toBeTruthy();
    expect(screen.getByText("Target")).toBeTruthy();
  });

  it("steps the target up and down", () => {
    const onChange = vi.fn();
    render(
      <HeroMetrics
        counters={counters}        agentsTarget={20}
        onAgentsTargetChange={onChange}
      />,
    );
    fireEvent.click(screen.getByTestId("agents-target-up"));
    expect(onChange).toHaveBeenLastCalledWith(21);
    fireEvent.click(screen.getByTestId("agents-target-down"));
    expect(onChange).toHaveBeenLastCalledWith(19);
  });

  it("anchors the first step on the actual count when no target exists", () => {
    const onChange = vi.fn();
    render(
      <HeroMetrics
        counters={counters}        agentsTarget={null}
        onAgentsTargetChange={onChange}
      />,
    );
    expect(screen.getByTestId("agents-target-value").textContent).toBe("–");
    fireEvent.click(screen.getByTestId("agents-target-up"));
    expect(onChange).toHaveBeenLastCalledWith(5); // active 4 + 1
  });

  it("never steps the target below zero and marks zero as paused", () => {
    const onChange = vi.fn();
    render(
      <HeroMetrics
        counters={counters}        agentsTarget={0}
        onAgentsTargetChange={onChange}
      />,
    );
    expect(screen.getByText("Target (paused)")).toBeTruthy();
    fireEvent.click(screen.getByTestId("agents-target-down"));
    expect(onChange).toHaveBeenLastCalledWith(0);
  });

  it("falls back to the plain active-agents tile without the control", () => {
    render(<HeroMetrics counters={counters} />);
    expect(screen.getByTestId("active-agents-value").textContent).toBe("4");
    expect(screen.queryByTestId("agents-target-up")).toBeNull();
  });
});

describe("<HeroMetrics /> output tok/sec tile", () => {
  it("renders the live rate to one decimal, with durable avg and max below", () => {
    render(
      <HeroMetrics
        counters={counters({ active: 3 })}
        liveTokensPerSec={146.04}
        peakTokensPerSec={210.5}
        // avg = total output tokens / active elapsed seconds = 8740 / 100.
        activeElapsedSeconds={100}
        activeAnchorTs={null}
        totalOutputTokens={8740}
      />,
    );
    expect(screen.getByTestId("output-tokens-value").textContent).toBe("146.0");
    expect(screen.getByTestId("output-tokens-avg").textContent).toBe("avg 87.4");
    expect(screen.getByTestId("output-tokens-max").textContent).toBe("max 210.5");
  });

  it("keeps avg and max after the run finishes, with the live rate at —", () => {
    // A finished run (or a fresh reload): no agent active, so the live rate is
    // null, but the file-derived avg/max still read off the snapshot.
    render(
      <HeroMetrics
        counters={counters()}
        liveTokensPerSec={null}
        peakTokensPerSec={142}
        activeElapsedSeconds={100}
        activeAnchorTs={null}
        totalOutputTokens={8740}
      />,
    );
    expect(screen.getByTestId("output-tokens-value").textContent).toBe("—");
    expect(screen.getByTestId("output-tokens-avg").textContent).toBe("avg 87.4");
    expect(screen.getByTestId("output-tokens-max").textContent).toBe("max 142.0");
  });

  it("renders the not-yet-available placeholder when nothing has run", () => {
    render(
      <HeroMetrics
        counters={counters()}
        liveTokensPerSec={null}
        peakTokensPerSec={null}
      />,
    );
    expect(screen.getByTestId("output-tokens-value").textContent).toBe("—");
    expect(screen.getByTestId("output-tokens-avg").textContent).toBe("avg —");
    expect(screen.getByTestId("output-tokens-max").textContent).toBe("max —");
  });

  it("shows avg as the headline and max below when the backend has no live rate", () => {
    // A non-streaming backend (oh / ct): liveRateSupported=false. The live rate
    // is never available, so the durable average becomes the headline and the
    // peak the secondary readout — no permanent "—".
    render(
      <HeroMetrics
        counters={counters({ active: 2 })}
        liveRateSupported={false}
        liveTokensPerSec={null}
        peakTokensPerSec={56.8}
        // avg = total output tokens / active elapsed = 8410 / 100 = 84.1.
        activeElapsedSeconds={100}
        activeAnchorTs={null}
        totalOutputTokens={8410}
      />,
    );
    expect(screen.getByText("Avg tok/sec")).toBeTruthy();
    expect(screen.getByTestId("output-tokens-value").textContent).toBe("84.1");
    expect(screen.getByTestId("output-tokens-max").textContent).toBe("max 56.8");
    // The streaming layout's separate "avg …" sub-readout is gone — the average
    // is the headline now.
    expect(screen.queryByTestId("output-tokens-avg")).toBeNull();
  });
});

describe("<HeroMetrics /> total output tokens tile", () => {
  it("renders the running total in millions to two decimals", () => {
    render(
      <HeroMetrics
        counters={counters()}        totalOutputTokens={1_234_567}
      />,
    );
    expect(screen.getByTestId("total-output-tokens-value").textContent).toBe(
      "1.23",
    );
  });

  it("defaults to 0.00 before any tokens are generated", () => {
    render(<HeroMetrics counters={counters()} />);
    expect(screen.getByTestId("total-output-tokens-value").textContent).toBe(
      "0.00",
    );
  });
});

describe("<HeroMetrics /> progress tile", () => {
  const progress: Record<string, ProgressMetric> = {
    "tests passing": {
      value: 1234,
      series: [
        ["2026-05-27T14:00:00.000Z", 10],
        ["2026-05-27T14:01:00.000Z", 600],
        ["2026-05-27T14:02:00.000Z", 1234],
      ],
    },
    // A second probe to prove only the first (primary) one renders.
    coverage: {
      value: 88,
      series: [
        ["2026-05-27T14:00:00.000Z", 80],
        ["2026-05-27T14:02:00.000Z", 88],
      ],
    },
  };

  it("renders the primary metric name, value, and a sparkline", () => {
    render(<HeroMetrics counters={counters()} progress={progress} />);
    expect(screen.getByText("tests passing")).toBeTruthy();
    expect(screen.getByTestId("progress-value").textContent).toBe("1,234");
    expect(screen.getByTestId("progress-sparkline")).toBeTruthy();
    expect(screen.getByTestId("sparkline-polyline")).toBeTruthy();
    // Only the first probe surfaces as a tile.
    expect(screen.queryByText("coverage")).toBeNull();
  });

  it("renders no progress card when progress is absent", () => {
    render(<HeroMetrics counters={counters()} />);
    expect(screen.queryByTestId("progress-value")).toBeNull();
    expect(screen.queryByTestId("progress-sparkline")).toBeNull();
  });

  it("renders no progress card when progress is an empty map", () => {
    render(<HeroMetrics counters={counters()} progress={{}} />);
    expect(screen.queryByTestId("progress-value")).toBeNull();
    expect(screen.queryByTestId("progress-sparkline")).toBeNull();
  });
});
