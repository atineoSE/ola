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
 * Continuity: when no agent advances in a given window (all between turns, or
 * the run drained) the windowed value is `null`; the hook then holds the last
 * non-null reading so the tile freezes instead of flipping to a placeholder —
 * the same pattern as the elapsed clock. A project switch resets both the
 * carried samples and the held value so the new project starts fresh.
 */

import { useEffect, useRef, useState } from "react";

import { windowedTokensPerSec } from "../snapshot";
import type { MetricSample, TaskState } from "../snapshot";

export function useOutputTokensPerSec(
  tasks: TaskState[],
  project: string | null,
): number | null {
  const prevSamples = useRef<Record<string, MetricSample>>({});
  const prevProject = useRef<string | null>(project);
  const [value, setValue] = useState<number | null>(null);

  useEffect(() => {
    if (prevProject.current !== project) {
      // Folder switch: drop the previous project's samples and frozen reading.
      prevProject.current = project;
      prevSamples.current = {};
      setValue(null);
    }
    const { value: windowed, samples } = windowedTokensPerSec(
      prevSamples.current,
      tasks,
    );
    prevSamples.current = samples;
    // Hold the last non-null reading across reporting gaps.
    if (windowed !== null) setValue(windowed);
  }, [tasks, project]);

  return value;
}
