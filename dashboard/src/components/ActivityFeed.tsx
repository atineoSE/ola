/*
 * Activity Feed — scrolling sidebar of recent `complete` events.
 *
 * Built so a viewer glancing at the dashboard mid-run can read what just
 * finished without watching the heatmap pixel-by-pixel. Entries are
 * newest-first; the store caps retention at `ACTIVITY_FEED_LIMIT` so the
 * DOM stays bounded across a long run.
 *
 * Each row shows the free-text `task_text`, the `agent_backend` · `folder`
 * context from the envelope, the completed attempt's generation throughput
 * (tokens/sec) as a badge, and its total `output_tokens`.
 */

import type { ActivityEntry } from "../snapshot";
import { readMetrics } from "../format";

export interface ActivityFeedProps {
  entries: readonly ActivityEntry[];
}

export function ActivityFeed({ entries }: ActivityFeedProps) {
  return (
    <section
      aria-label="activity feed"
      data-testid="activity-feed"
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-bg-panel"
    >
      <header className="shrink-0 border-b border-border px-4 py-3 text-sm uppercase tracking-wider text-text-muted">
        Recently completed
      </header>
      {entries.length === 0 ? (
        <div
          data-testid="activity-feed-empty"
          className="px-4 py-10 text-center text-sm text-text-muted"
        >
          No tasks completed yet.
        </div>
      ) : (
        <ol
          data-testid="activity-feed-list"
          className="min-h-0 flex-1 divide-y divide-border overflow-y-auto"
        >
          {entries.map((entry) => (
            <ActivityRow key={`${entry.task_id}|${entry.ts}`} entry={entry} />
          ))}
        </ol>
      )}
    </section>
  );
}

interface ActivityRowProps {
  entry: ActivityEntry;
}

function ActivityRow({ entry }: ActivityRowProps) {
  const metrics = readMetrics(entry.data);
  return (
    <li
      data-testid={`activity-row-${entry.task_id}`}
      className="flex flex-col gap-1 px-4 py-3 text-sm"
    >
      <div className="flex items-baseline justify-between gap-3">
        <span className="truncate text-text" title={entry.task_text}>
          {entry.task_text}
        </span>
        {metrics && (
          <span className="shrink-0 font-mono tabular-nums text-text-muted">
            {metrics.tokens_per_sec.toFixed(1)} tok/s
          </span>
        )}
      </div>
      <div className="text-xs text-text-muted">
        {entry.agent_backend} · {entry.folder}
        {metrics && ` · ${metrics.output_tokens.toLocaleString()} tok`}
      </div>
    </li>
  );
}
