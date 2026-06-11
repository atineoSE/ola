import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useElapsedSeconds } from "./useElapsedSeconds";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-05-27T14:03:20.000Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useElapsedSeconds", () => {
  it("returns null until a first_started_ts is provided", () => {
    const { result } = renderHook(() => useElapsedSeconds(null));
    expect(result.current).toBeNull();
  });

  it("computes the initial elapsed value from the start timestamp", () => {
    const { result } = renderHook(() =>
      useElapsedSeconds("2026-05-27T14:03:15.000Z"),
    );
    expect(result.current).toBe(5);
  });

  it("ticks once per second", () => {
    const { result } = renderHook(() =>
      useElapsedSeconds("2026-05-27T14:03:15.000Z"),
    );
    expect(result.current).toBe(5);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(6);
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(result.current).toBe(9);
  });

  it("stops ticking once unmounted", () => {
    const { result, unmount } = renderHook(() =>
      useElapsedSeconds("2026-05-27T14:03:15.000Z"),
    );
    unmount();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(5);
  });

  it("re-anchors when the start timestamp changes", () => {
    const { result, rerender } = renderHook(
      ({ ts }: { ts: string | null }) => useElapsedSeconds(ts),
      { initialProps: { ts: "2026-05-27T14:03:15.000Z" } },
    );
    expect(result.current).toBe(5);
    rerender({ ts: "2026-05-27T14:03:18.000Z" });
    expect(result.current).toBe(2);
  });

  it("clamps negative deltas (start in the future) to zero", () => {
    const { result } = renderHook(() =>
      useElapsedSeconds("2026-05-27T14:03:25.000Z"),
    );
    expect(result.current).toBe(0);
  });
});

describe("useElapsedSeconds — frozen at stopTs", () => {
  it("returns stop - start and ignores wall-clock time", () => {
    const { result } = renderHook(() =>
      useElapsedSeconds("2026-05-27T14:03:00.000Z", "2026-05-27T14:03:10.000Z"),
    );
    expect(result.current).toBe(10);
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(10); // frozen — the run is over
  });

  it("freezes when stopTs arrives mid-run and resumes if it clears", () => {
    const { result, rerender } = renderHook(
      ({ stop }: { stop: string | null }) =>
        useElapsedSeconds("2026-05-27T14:03:15.000Z", stop),
      { initialProps: { stop: null as string | null } },
    );
    expect(result.current).toBe(5);
    rerender({ stop: "2026-05-27T14:03:18.000Z" });
    expect(result.current).toBe(3);
    // A new attempt clears the stop (e.g. retry restarts the run).
    rerender({ stop: null });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(6);
  });
});
