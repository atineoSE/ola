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
 * A per-task throughput sample carried between polls so the fleet rate can be
 * computed as a *window* rather than a lifetime average. Keyed by `task_id`.
 */
export interface MetricSample {
  output_tokens: number;
  decode_ms: number;
}

/**
 * Windowed fleet output throughput (tokens/sec): the **sum** over currently
 * active agents of each one's `Δoutput_tokens / Δdecode_ms` since the previous
 * poll. This tracks *current* throughput, unlike the emitted lifetime-average
 * `tokens_per_sec`, which barely moves once an attempt has run a while — so the
 * hero tile actually responds to the run. SCHEMA.md explicitly recommends
 * plotting the delta between consecutive observed events rather than the
 * lifetime average.
 *
 * Pure and stateless: it takes the previous poll's samples (`prev`) and the
 * current tasks, and returns the new rate plus the samples to carry forward.
 * `value` is `null` when no active agent *advanced* this window (pre-run, all
 * agents between turns, or the run drained); the caller decides whether to hold
 * the last reading. `samples` only ever contains the current active tasks, so
 * stale task ids never accumulate across a long run.
 */
export function windowedTokensPerSec(
  prev: Record<string, MetricSample>,
  tasks: Record<string, TaskState> | TaskState[],
): { value: number | null; samples: Record<string, MetricSample> } {
  const list = Array.isArray(tasks) ? tasks : Object.values(tasks);
  const samples: Record<string, MetricSample> = {};
  let sum = 0;
  let advanced = false;
  for (const t of list) {
    if (t.status !== "started" && t.status !== "working") continue;
    const m = readMetrics(t.data);
    if (m == null) continue;
    samples[t.task_id] = {
      output_tokens: m.output_tokens,
      decode_ms: m.decode_ms,
    };
    const p = prev[t.task_id];
    if (p == null) continue; // first sighting — need two points for a rate
    const dTokens = m.output_tokens - p.output_tokens;
    const dDecodeMs = m.decode_ms - p.decode_ms;
    // Guard against a fresh attempt (counters reset → negative delta) and the
    // no-progress case (dDecodeMs === 0) that would divide by zero.
    if (dDecodeMs > 0 && dTokens >= 0) {
      sum += dTokens / (dDecodeMs / 1000);
      advanced = true;
    }
  }
  return { value: advanced ? sum : null, samples };
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
