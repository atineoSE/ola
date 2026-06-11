import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useActiveElapsed } from "./useActiveElapsed";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-05-27T14:03:25.000Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useActiveElapsed", () => {
  it("returns null until any agent has run (base null)", () => {
    const { result } = renderHook(() => useActiveElapsed(null, null));
    expect(result.current).toBeNull();
  });

  it("freezes at the base seconds when idle (no anchor)", () => {
    const { result } = renderHook(() => useActiveElapsed(42, null));
    expect(result.current).toBe(42);
    // Wall time passes, but with no anchor the value must not move.
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(42);
  });

  it("adds the live tail (now - anchor) on top of the base while running", () => {
    // anchor 10s before the system time, base 5 → 15s.
    const { result } = renderHook(() =>
      useActiveElapsed(5, "2026-05-27T14:03:15.000Z"),
    );
    expect(result.current).toBe(15);
  });

  it("ticks the tail forward once per second while running", () => {
    const { result } = renderHook(() =>
      useActiveElapsed(0, "2026-05-27T14:03:25.000Z"),
    );
    expect(result.current).toBe(0);
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(result.current).toBe(3);
  });

  it("falls back to the base when the anchor is unparseable", () => {
    const { result } = renderHook(() => useActiveElapsed(8, "not-a-date"));
    expect(result.current).toBe(8);
  });
});
