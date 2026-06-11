/*
 * Metrics Panel — generic, schema-agnostic view of whatever tasks publish
 * under the opaque `data` payload.
 *
 * v2 dropped the typed verifier deltas (baseline/verified error counts,
 * coverage transitions), so the dashboard no longer knows what any metric
 * means. This panel simply lists every task carrying non-empty `data` and
 * renders its key/value pairs as chips. Tasks with no metrics are omitted;
 * an all-empty run shows the empty state.
 */

import { memo } from "react";

import type { TaskState } from "../snapshot";
import { metricEntries, type MetricEntry } from "../format";

export interface MetricsPanelProps {
  tasks: TaskState[];
}

interface MetricsRow {
  task_id: string;
  task_text: string;
  agent_backend: string;
  metrics: MetricEntry[];
}

export function MetricsPanel({ tasks }: MetricsPanelProps) {
  const rows: MetricsRow[] = tasks
    .map((t) => ({
      task_id: t.task_id,
      task_text: t.task_text,
      agent_backend: t.agent_backend,
      metrics: metricEntries(t.data),
    }))
    .filter((r) => r.metrics.length > 0);

  return (
    <section
      aria-label="metrics"
      data-testid="metrics-panel"
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-bg-panel"
    >
      <header className="shrink-0 border-b border-border px-4 py-3 text-sm uppercase tracking-wider text-text-muted">
        Metrics
      </header>
      {rows.length === 0 ? (
        <div
          data-testid="metrics-panel-empty"
          className="px-4 py-10 text-center text-sm text-text-muted"
        >
          No metrics reported yet.
        </div>
      ) : (
        <ul
          data-testid="metrics-panel-list"
          className="min-h-0 flex-1 divide-y divide-border overflow-y-auto"
        >
          {rows.map((row) => (
            <MetricsRowItem key={row.task_id} row={row} />
          ))}
        </ul>
      )}
    </section>
  );
}

const MetricsRowItem = memo(function MetricsRowItem({
  row,
}: {
  row: MetricsRow;
}) {
  return (
    <li
      data-testid={`metrics-row-${row.task_id}`}
      className="flex flex-col gap-2 px-4 py-3 text-sm"
    >
      <div className="flex items-baseline justify-between gap-3">
        <span className="truncate text-text" title={row.task_text}>
          {row.task_text}
        </span>
        <span className="shrink-0 font-mono text-xs uppercase text-text-muted">
          {row.agent_backend}
        </span>
      </div>
      <div className="flex flex-wrap gap-2">
        {row.metrics.map((m) => (
          <span
            key={m.key}
            data-testid={`metric-chip-${row.task_id}-${m.key}`}
            className="rounded bg-border px-2 py-0.5 font-mono text-xs text-text"
          >
            {m.key}: {m.value}
          </span>
        ))}
      </div>
    </li>
  );
});
