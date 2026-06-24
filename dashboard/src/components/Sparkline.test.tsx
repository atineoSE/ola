import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Sparkline } from "./Sparkline";

afterEach(() => {
  cleanup();
});

describe("<Sparkline />", () => {
  it("draws a polyline normalized over the value range", () => {
    // min 0 → bottom (y = height), max 10 → top (y = 0), midpoint → middle.
    render(<Sparkline values={[0, 5, 10]} width={100} height={20} />);
    const line = screen.getByTestId("sparkline-polyline");
    expect(line.getAttribute("points")).toBe("0.00,20.00 50.00,10.00 100.00,0.00");
  });

  it("pins a flat series to the bottom rather than dividing by zero", () => {
    // Zero span: every point maps to y = height (no NaN, no Infinity).
    render(<Sparkline values={[7, 7, 7]} width={100} height={20} />);
    const line = screen.getByTestId("sparkline-polyline");
    expect(line.getAttribute("points")).toBe(
      "0.00,20.00 50.00,20.00 100.00,20.00",
    );
  });

  it("renders nothing with fewer than two points", () => {
    const { container } = render(<Sparkline values={[42]} />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("sparkline-polyline")).toBeNull();
  });

  it("forwards a data-testid onto the svg", () => {
    render(<Sparkline values={[1, 2]} data-testid="my-spark" />);
    expect(screen.getByTestId("my-spark").tagName.toLowerCase()).toBe("svg");
  });
});
