/*
 * Pure helpers over a dashboard `Snapshot`.
 *
 * The snapshot is built server-side by `ola.monitor.data.build_snapshot` and
 * replaces the whole client state on each poll (no incremental event folding
 * — the files are the source of truth and every poll re-reads them). These
 * helpers derive per-project views the App scopes to the picked folder.
 */

import { readMetrics } from "../format";
import type { Counters, Snapshot, TaskState } from "./types";

/**
 * Activity Feed retention, mirrored by the server's `_ACTIVITY_LIMIT`. 50 is
 * enough to fill a sidebar on a 1080p projector without unbounded growth.
 */
export const ACTIVITY_FEED_LIMIT = 50;

export const EMPTY_COUNTERS: Counters = {
  total_tasks: 0,
  completed: 0,
  failed: 0,
  active: 0,
};

export const EMPTY_SNAPSHOT: Snapshot = {
  first_started_ts: null,
  counters: EMPTY_COUNTERS,
  tasks: {},
  folders: {},
  activity: [],
};

/**
 * Total output-token throughput (tokens/sec) across the fleet right now:
 * the **sum** of each currently active agent's `tokens_per_sec` — those in
 * `started`/`working` whose latest payload carried a `metrics` block.
 *
 * Returns `null` when no active agent is reporting (pre-run, between waves,
 * or after the run drains). Display continuity is the caller's concern: the
 * App holds the last non-null reading so the tile freezes rather than
 * flipping back to a placeholder (same pattern as the elapsed clock).
 */
export function outputTokensPerSec(
  tasks: Record<string, TaskState> | TaskState[],
): number | null {
  const list = Array.isArray(tasks) ? tasks : Object.values(tasks);
  let sum = 0;
  let n = 0;
  for (const t of list) {
    if (t.status !== "started" && t.status !== "working") continue;
    const m = readMetrics(t.data);
    if (m == null) continue;
    sum += m.tokens_per_sec;
    n += 1;
  }
  return n === 0 ? null : sum;
}

export function recomputeCounters(tasks: Record<string, TaskState>): Counters {
  let completed = 0;
  let failed = 0;
  let active = 0;

  for (const ts of Object.values(tasks)) {
    if (ts.status === "complete") {
      completed += 1;
    } else if (ts.status === "failed") {
      failed += 1;
    } else if (ts.status !== "pending") {
      // "pending" tasks have no agent yet — only started/working are active.
      active += 1;
    }
  }

  return {
    total_tasks: Object.keys(tasks).length,
    completed,
    failed,
    active,
  };
}
