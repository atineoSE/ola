import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HeroMetrics } from "./HeroMetrics";
import type { Counters } from "../snapshot";

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
        })}
        firstStartedTs={null}
      />,
    );
    // Failed attempts return their task to the pool — not finished work.
    expect(screen.getByTestId("tasks-completed-value").textContent).toBe(
      "12 / 1,045",
    );
    expect(screen.getByTestId("active-agents-value").textContent).toBe("4");
    expect(screen.getByTestId("failed-value").textContent).toBe("1");
  });

  it("shows --:-- for elapsed time before the first started event", () => {
    render(
      <HeroMetrics counters={counters({ total_tasks: 5 })} firstStartedTs={null} />,
    );
    expect(screen.getByTestId("elapsed-value").textContent).toBe("--:--");
  });

  it("renders elapsed time anchored to first_started_ts", () => {
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 5 })}
        firstStartedTs="2026-05-27T14:03:15.000Z"
      />,
    );
    expect(screen.getByTestId("elapsed-value").textContent).toBe("00:10");
  });

  it("sets progress-bar width to the completed fraction of total_tasks", () => {
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 1000, completed: 240, failed: 10 })}
        firstStartedTs={null}
      />,
    );
    // failed: 10 is excluded — those tasks went back to the pool.
    const fill = screen.getByTestId("progress-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("24%");
    const bar = screen.getByRole("progressbar");
    expect(bar.getAttribute("aria-valuenow")).toBe("24");
  });

  it("does not divide by zero when total_tasks is 0", () => {
    render(<HeroMetrics counters={counters()} firstStartedTs={null} />);
    const fill = screen.getByTestId("progress-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("0%");
  });

  it("clamps a runaway progress fraction at 100%", () => {
    render(
      <HeroMetrics
        counters={counters({ total_tasks: 10, completed: 12, failed: 0 })}
        firstStartedTs={null}
      />,
    );
    const fill = screen.getByTestId("progress-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("100%");
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
        counters={counters}
        firstStartedTs={null}
        agentsTarget={20}
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
        counters={counters}
        firstStartedTs={null}
        agentsTarget={20}
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
        counters={counters}
        firstStartedTs={null}
        agentsTarget={null}
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
        counters={counters}
        firstStartedTs={null}
        agentsTarget={0}
        onAgentsTargetChange={onChange}
      />,
    );
    expect(screen.getByText("Target (paused)")).toBeTruthy();
    fireEvent.click(screen.getByTestId("agents-target-down"));
    expect(onChange).toHaveBeenLastCalledWith(0);
  });

  it("falls back to the plain active-agents tile without the control", () => {
    render(<HeroMetrics counters={counters} firstStartedTs={null} />);
    expect(screen.getByTestId("active-agents-value").textContent).toBe("4");
    expect(screen.queryByTestId("agents-target-up")).toBeNull();
  });
});

describe("<HeroMetrics /> output tok/sec tile", () => {
  it("renders the current rate to one decimal, with avg and max below", () => {
    render(
      <HeroMetrics
        counters={counters({ active: 3 })}
        firstStartedTs={null}
        outputTokensPerSec={{ current: 146.04, avg: 120, max: 210.5 }}
      />,
    );
    expect(screen.getByTestId("output-tokens-value").textContent).toBe("146.0");
    expect(screen.getByTestId("output-tokens-avg").textContent).toBe("avg 120.0");
    expect(screen.getByTestId("output-tokens-max").textContent).toBe("max 210.5");
  });

  it("renders the not-yet-available placeholder when null", () => {
    render(
      <HeroMetrics
        counters={counters()}
        firstStartedTs={null}
        outputTokensPerSec={null}
      />,
    );
    expect(screen.getByTestId("output-tokens-value").textContent).toBe("—");
    expect(screen.getByTestId("output-tokens-avg").textContent).toBe("avg —");
    expect(screen.getByTestId("output-tokens-max").textContent).toBe("max —");
  });
});

describe("<HeroMetrics /> total output tokens tile", () => {
  it("renders the running total in millions to two decimals", () => {
    render(
      <HeroMetrics
        counters={counters()}
        firstStartedTs={null}
        totalOutputTokens={1_234_567}
      />,
    );
    expect(screen.getByTestId("total-output-tokens-value").textContent).toBe(
      "1.23",
    );
  });

  it("defaults to 0.00 before any tokens are generated", () => {
    render(<HeroMetrics counters={counters()} firstStartedTs={null} />);
    expect(screen.getByTestId("total-output-tokens-value").textContent).toBe(
      "0.00",
    );
  });
});
