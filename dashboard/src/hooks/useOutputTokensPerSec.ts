/*
 * Live fleet output throughput for the hero tile.
 *
 * The dashboard is stateless across polls (each `/api/snapshot` replaces the
 * whole view), but a *windowed* rate needs two consecutive samples. This hook
 * is the small amount of client-side memory that makes that possible: it keeps
 * the previous poll's per-task metrics in a ref and feeds them to the pure
 * `windowedTokensPerSec`, so the tile shows current throughput rather than the
 * slowly-moving lifetime average the events carry.
 *
 * Alongside the current rate it keeps the run's **average** (running mean of
 * the windowed readings) and **peak** (max reading), so the tile can show where
 * the live number sits relative to the run. All three reset on a project switch.
 *
 * Continuity: when no agent advances in a given window (all between turns, or
 * the run drained) the windowed value is `null`; the hook then holds the last
 * non-null reading for `current` so the tile freezes instead of flipping to a
 * placeholder — the same pattern as the elapsed clock — while avg/max keep their
 * accumulated values. A project switch resets the carried samples and all three
 * readings so the new project starts fresh.
 */

import { useEffect, useRef, useState } from "react";

import { windowedTokensPerSec } from "../snapshot";
import type { MetricSample, TaskState } from "../snapshot";

export interface OutputTokensPerSec {
  /** Latest windowed rate, held across reporting gaps. `null` before any. */
  current: number | null;
  /** Running mean of the windowed readings so far. `null` before any. */
  avg: number | null;
  /** Peak windowed reading so far. `null` before any. */
  max: number | null;
}

const EMPTY: OutputTokensPerSec = { current: null, avg: null, max: null };

export function useOutputTokensPerSec(
  tasks: TaskState[],
  project: string | null,
): OutputTokensPerSec {
  const prevSamples = useRef<Record<string, MetricSample>>({});
  const prevProject = useRef<string | null>(project);
  // Running accumulators for the average; kept in a ref so re-renders don't
  // reset them and they survive reporting gaps.
  const acc = useRef<{ sum: number; count: number; max: number }>({
    sum: 0,
    count: 0,
    max: 0,
  });
  const [value, setValue] = useState<OutputTokensPerSec>(EMPTY);

  useEffect(() => {
    if (prevProject.current !== project) {
      // Folder switch: drop the previous project's samples and all readings.
      prevProject.current = project;
      prevSamples.current = {};
      acc.current = { sum: 0, count: 0, max: 0 };
      setValue(EMPTY);
    }
    const { value: windowed, samples } = windowedTokensPerSec(
      prevSamples.current,
      tasks,
    );
    prevSamples.current = samples;
    if (windowed !== null) {
      const a = acc.current;
      a.sum += windowed;
      a.count += 1;
      a.max = Math.max(a.max, windowed);
      // Hold the last non-null reading for `current` across reporting gaps.
      setValue({ current: windowed, avg: a.sum / a.count, max: a.max });
    }
  }, [tasks, project]);

  return value;
}
