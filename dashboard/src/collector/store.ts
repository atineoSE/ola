/*
 * Local mirror of the collector's aggregation state, folded from the
 * snapshot + live lifecycle events delivered over SSE.
 *
 * The folding rules here intentionally match
 * `collector/src/collector/state.py::AggregationState.apply` so a freshly
 * subscribed client converges to the same numbers the server reports in
 * `GET /stream`'s opening `snapshot`. The unit of work is a **task**
 * (keyed by `task_id`); `data` is opaque and retained verbatim for the UI
 * to surface generically.
 */

import { readMetrics } from "../format";
import type {
  ActivityEntry,
  Counters,
  FolderClock,
  LifecycleEvent,
  ManifestMessage,
  Snapshot,
  TaskState,
} from "./types";

/**
 * Activity Feed retention. 50 is enough to fill a sidebar on a 1080p
 * projector without unbounded DOM growth across a long run.
 */
export const ACTIVITY_FEED_LIMIT = 50;

export interface CollectorStore {
  first_started_ts: string | null;
  counters: Counters;
  tasks: Record<string, TaskState>;
  /** Per-folder run clocks (first started / last terminal event). */
  folders: Record<string, FolderClock>;
  /**
   * Most recent `complete` events, newest first, capped at
   * `ACTIVITY_FEED_LIMIT`. Reset on snapshot apply since the snapshot
   * carries no completion-order information.
   */
  activity: ActivityEntry[];
  /**
   * `${agent_id}|${attempt}` → highest `seq` already folded.
   * Used to drop duplicate / out-of-order POSTs that the collector
   * already rejected but might still arrive via reconnects.
   */
  maxSeq: Record<string, number>;
}

export const EMPTY_COUNTERS: Counters = {
  total_tasks: 0,
  completed: 0,
  failed: 0,
  active: 0,
};

export const EMPTY_STORE: CollectorStore = {
  first_started_ts: null,
  counters: EMPTY_COUNTERS,
  tasks: {},
  folders: {},
  activity: [],
  maxSeq: {},
};

/**
 * Total output-token throughput (tokens/sec) across the fleet right now:
 * the **sum** of each currently active agent's `tokens_per_sec` — those in
 * `started`/`working` whose latest event carried a `metrics` block.
 *
 * Returns `null` when no active agent is reporting (pre-run, between waves,
 * or after the run drains). Display continuity is the caller's concern: the
 * App holds the last non-null reading so the tile freezes rather than
 * flipping back to a placeholder (same pattern as the elapsed clock).
 *
 * Each agent's lifetime `tokens_per_sec` is self-contained per event (robust
 * to dropped SSE deltas) and recoverable from the snapshot alone.
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

export function fromSnapshot(snapshot: Snapshot): CollectorStore {
  return {
    first_started_ts: snapshot.first_started_ts,
    counters: snapshot.counters,
    tasks: { ...snapshot.tasks },
    folders: { ...(snapshot.folders ?? {}) },
    activity: [],
    maxSeq: {},
  };
}

/**
 * Fold a single lifecycle event into the store, returning a new store
 * value. Returns the same reference unchanged if the event is a
 * duplicate or out-of-order delivery (per the `(agent_id, attempt, seq)`
 * monotonicity rule from SCHEMA.md).
 */
export function applyEvent(
  store: CollectorStore,
  event: LifecycleEvent,
): CollectorStore {
  const dedupeKey = `${event.agent_id}|${event.attempt}`;
  const last = store.maxSeq[dedupeKey];
  if (last !== undefined && event.seq <= last) {
    return store;
  }

  const maxSeq = { ...store.maxSeq, [dedupeKey]: event.seq };

  const first_started_ts =
    event.status === "started" && store.first_started_ts === null
      ? event.ts
      : store.first_started_ts;

  const next: TaskState = {
    task_id: event.task_id,
    task_text: event.task_text,
    folder: event.folder,
    agent_backend: event.agent_backend,
    status: event.status,
    attempt: event.attempt,
    // `data` is opaque; retain the latest payload verbatim.
    data: { ...event.data },
  };

  const tasks = { ...store.tasks, [event.task_id]: next };

  // Per-folder run clock: earliest started, latest terminal. RFC 3339
  // timestamps with fixed precision compare correctly as strings.
  const clock: FolderClock = store.folders[event.folder] ?? {
    first_started_ts: null,
    last_terminal_ts: null,
  };
  const folders: Record<string, FolderClock> = {
    ...store.folders,
    [event.folder]: {
      first_started_ts:
        event.status === "started" &&
        (clock.first_started_ts === null || event.ts < clock.first_started_ts)
          ? event.ts
          : clock.first_started_ts,
      last_terminal_ts:
        (event.status === "complete" || event.status === "failed") &&
        (clock.last_terminal_ts === null || event.ts > clock.last_terminal_ts)
          ? event.ts
          : clock.last_terminal_ts,
    },
  };

  const activity =
    event.status === "complete"
      ? prependActivity(store.activity, {
          task_id: next.task_id,
          task_text: next.task_text,
          folder: next.folder,
          agent_backend: next.agent_backend,
          ts: event.ts,
          data: next.data,
        })
      : store.activity;

  return {
    first_started_ts,
    tasks,
    folders,
    activity,
    counters: recomputeCounters(tasks),
    maxSeq,
  };
}

/**
 * Fold a `manifest` message into the store: every announced task not yet
 * known is added as `pending`, in manifest (dispatch) order. Tasks that
 * already have live state are left untouched, so a late or re-posted
 * manifest never downgrades progress.
 */
export function applyManifest(
  store: CollectorStore,
  manifest: ManifestMessage,
): CollectorStore {
  const tasks = { ...store.tasks };
  for (const t of manifest.tasks) {
    if (tasks[t.task_id] !== undefined) continue;
    tasks[t.task_id] = {
      task_id: t.task_id,
      task_text: t.task_text,
      folder: manifest.folder,
      agent_backend: manifest.agent_backend,
      status: "pending",
      attempt: 0,
      data: {},
    };
  }

  const existing = store.folders[manifest.folder] ?? {
    first_started_ts: null,
    last_terminal_ts: null,
  };
  const folders = {
    ...store.folders,
    [manifest.folder]: {
      ...existing,
      project: manifest.project || existing.project,
    },
  };

  return {
    ...store,
    tasks,
    folders,
    counters: recomputeCounters(tasks),
  };
}

function prependActivity(
  current: readonly ActivityEntry[],
  entry: ActivityEntry,
): ActivityEntry[] {
  const next = [entry, ...current];
  if (next.length > ACTIVITY_FEED_LIMIT) {
    next.length = ACTIVITY_FEED_LIMIT;
  }
  return next;
}
