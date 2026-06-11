/*
 * Types for the file-driven dashboard snapshot.
 *
 * The `ola-dashboard` server (`src/ola/dashboard/server.py`) re-parses the
 * agent folder on each request via `ola.monitor.data.build_snapshot` and
 * returns the `Snapshot` below as JSON. The shape mirrors Ola's event
 * envelope (`src/ola/events/SCHEMA.md`): work is free-text `task_text` plus a
 * stable `task_id`, with `folder` and `agent_backend` as context, and the
 * lifecycle is `started → working* → complete | failed` (`pending` for tasks
 * in the spine that no agent has picked up yet). `data` is an opaque,
 * status-specific payload the dashboard renders generically.
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
 * Dashboard task status: the Ola lifecycle plus `pending` for tasks present
 * in the `.ola/tasks.json` spine that no agent has started yet. `pending`
 * never appears as an event status — only in task state.
 */
export type TaskStatus = Status | "pending";

/**
 * The per-task row. The unit of work is a task (keyed by `task_id`). `data`
 * holds the latest opaque payload the task published — the dashboard renders
 * its key/value pairs generically.
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
  /** Display name for the folder — the plan subfolder name. */
  project?: string;
  /** Dominant agent backend driving the folder (`"cc"` / `"oh"` / `"cx"`);
   * empty before any event lands. Themes the dashboard and names the agent. */
  agent_backend?: string;
  /** Model names reported in the folder's STATS.jsonl, first-seen order.
   * Events don't carry the model, so the snapshot surfaces it from there. */
  models?: string[];
}

/**
 * A single completed-task row for the Activity Feed, built server-side from
 * the originating `complete` event (its throughput badge is read from
 * `data.metrics`). Newest-first, capped server-side.
 */
export interface ActivityEntry {
  task_id: string;
  task_text: string;
  folder: string;
  agent_backend: string;
  ts: string;
  data: Record<string, unknown>;
}

/**
 * The full dashboard snapshot, re-read from the agent folder on every poll.
 * Unlike the old collector's snapshot it carries `activity` directly, since
 * there is no live event stream to accumulate it from.
 */
export interface Snapshot {
  first_started_ts: string | null;
  counters: Counters;
  tasks: Record<string, TaskState>;
  folders: Record<string, FolderClock>;
  activity: ActivityEntry[];
}
