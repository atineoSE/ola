/*
 * Wire types for the agent → collector → dashboard contract.
 *
 * Mirrors the Ola envelope in Ola's `src/ola/events/SCHEMA.md` and the
 * snapshot shape produced by the collector's
 * `AggregationState._snapshot_locked` in
 * `parallel-refactor/collector/src/collector/state.py`.
 *
 * Work is free-text `task_text` plus a stable `task_id`, with `folder` and
 * `agent_backend` as context; the lifecycle is
 * `started → working* → complete | failed`. `data` is a status-specific
 * payload (typed in SCHEMA.md) the dashboard reads per the payload tables.
 */

export type Status = "started" | "working" | "complete" | "failed";

/**
 * Generation-throughput counters carried under `data.metrics` on
 * `working` / `complete` / `failed` events (see Ola's SCHEMA.md). Counters
 * are cumulative per attempt; `tokens_per_sec` is the lifetime average
 * (`output_tokens / (decode_ms / 1000)`, `0` when `decode_ms` is `0`).
 */
export interface Metrics {
  output_tokens: number;
  decode_ms: number;
  tokens_per_sec: number;
}

/**
 * Collector-level task status: the Ola v2 lifecycle plus `pending` for
 * tasks announced via a manifest but not yet picked up by any agent.
 * `pending` never appears as an event status — only in task state.
 */
export type TaskStatus = Status | "pending";

/**
 * The per-task aggregation row. The unit of work is a task (keyed by
 * `task_id`), not a file. `data` holds the latest opaque payload the task
 * published — the dashboard renders its key/value pairs generically.
 */
export interface TaskState {
  task_id: string;
  task_text: string;
  folder: string;
  agent_backend: string;
  status: TaskStatus;
  attempt: number;
  data: Record<string, unknown>;
}

export interface Counters {
  total_tasks: number;
  completed: number;
  failed: number;
  active: number;
}

/**
 * Run clock for one folder: anchored at the first `started` event,
 * advanced to the latest terminal (`complete` / `failed`) event. The
 * elapsed readout freezes at `last_terminal_ts` once every task in the
 * folder is done.
 */
export interface FolderClock {
  first_started_ts: string | null;
  last_terminal_ts: string | null;
  /** Display name for the folder's project — the source folder the harness
   * runs from (e.g. "yt-dlp"). Empty/absent when no manifest declared it. */
  project?: string;
}

export interface Snapshot {
  first_started_ts: string | null;
  counters: Counters;
  tasks: Record<string, TaskState>;
  /** Optional for older collectors that predate per-folder clocks. */
  folders?: Record<string, FolderClock>;
}

/**
 * Upfront announcement of a folder's full task list (`POST /manifest`,
 * re-broadcast over SSE as a `manifest` message). Tasks are in dispatch
 * order; the dashboard seeds them as `pending`.
 */
export interface ManifestMessage {
  folder: string;
  agent_backend: string;
  /** Source-folder display name; may be empty. */
  project?: string;
  tasks: Array<{ task_id: string; task_text: string }>;
}

/**
 * A single completed-task row displayed in the Activity Feed. Built from
 * the originating `complete` event plus the task's accumulated `data` at
 * the time the event was folded into the store (the row's throughput badge
 * is read from `data.metrics`).
 */
export interface ActivityEntry {
  task_id: string;
  task_text: string;
  folder: string;
  agent_backend: string;
  ts: string;
  data: Record<string, unknown>;
}

export interface LifecycleEvent {
  agent_id: string;
  attempt: number;
  seq: number;
  ts: string;
  folder: string;
  task_id: string;
  task_text: string;
  agent_backend: string;
  status: Status;
  data: Record<string, unknown>;
}
