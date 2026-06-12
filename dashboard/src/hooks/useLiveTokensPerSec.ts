/*
 * Live fleet output throughput for the hero tile — the *current* windowed rate
 * only. Avg and peak are no longer accumulated here: they're derived from the
 * durable `.ola/` files instead (the run's lifetime average from the elapsed
 * clock, and `peakTaskTokensPerSec` over the per-task metrics), so they survive
 * a finished run and a fresh page load — which client-side session accumulators
 * could not (they reset to "—" on reload).
 *
 * A windowed rate needs two consecutive samples, so this hook keeps the prior
 * poll's per-task metrics in a ref and feeds them to the pure
 * `windowedTokensPerSec`. The reading is held across a no-progress gap *while
 * ≥1 agent is still active* (between turns), so the tile doesn't flicker to a
 * placeholder mid-run; once no agent is active (the run finished or drained) it
 * returns `null` so the tile reads `—` — there is no live rate to report. The
 * carried samples and held reading reset on a project switch.
 */

import { useEffect, useRef, useState } from "react";

import { windowedTokensPerSec } from "../snapshot";
import type { MetricSample, TaskState } from "../snapshot";

export function useLiveTokensPerSec(
  tasks: TaskState[],
  project: string | null,
): number | null {
  const prevSamples = useRef<Record<string, MetricSample>>({});
  const prevProject = useRef<string | null>(project);
  // Last non-null reading, held across a no-progress gap while still running.
  const held = useRef<number | null>(null);
  const [value, setValue] = useState<number | null>(null);

  useEffect(() => {
    if (prevProject.current !== project) {
      // Folder switch: drop the previous project's samples and held reading.
      prevProject.current = project;
      prevSamples.current = {};
      held.current = null;
    }
    const anyActive = tasks.some(
      (t) => t.status === "started" || t.status === "working",
    );
    const { value: windowed, samples } = windowedTokensPerSec(
      prevSamples.current,
      tasks,
    );
    prevSamples.current = samples;

    let next: number | null;
    if (!anyActive) {
      // Finished or idle: nothing is decoding, so there is no live rate.
      held.current = null;
      next = null;
    } else if (windowed !== null) {
      held.current = windowed;
      next = windowed;
    } else {
      // Between turns but still active: hold the last reading, don't blank it.
      next = held.current;
    }
    // Bail out when unchanged (every poll re-runs this) so a settled rate
    // doesn't trigger a cascading re-render.
    setValue((prev) => (prev === next ? prev : next));
  }, [tasks, project]);

  return value;
}
