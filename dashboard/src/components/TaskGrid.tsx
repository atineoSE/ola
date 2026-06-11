/*
 * Task Grid Heatmap — one cell per work item, colored by lifecycle status.
 *
 * The dashboard's centerpiece during the live demo: every work item from
 * the run's manifest is visible from the start as a gray (pending) box,
 * and cells light up in dispatch order — yellow while an agent works,
 * green on completion — so the wave sweeps across the grid. A failed
 * attempt leaves its checkbox unticked, so the task returns to the pool:
 * the cell goes back to unclaimed gray (the tooltip keeps the why) until
 * the harness claims it again.
 *
 * Cells are NOT reordered: the incoming task list keeps the collector's
 * insertion order, which is the manifest (files.txt) order. The grid sizes
 * its cells to occupy all available panel space — it measures itself with
 * a ResizeObserver and picks the column count that maximizes cell size
 * while fitting every item.
 *
 * Each cell is memoized on primitive props so a single arriving event only
 * re-renders the cells it touched; hovering shows the item name and a
 * friendly status (pending / running / succeeded / failed).
 */

import { memo, useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";

import type { TaskState, TaskStatus } from "../snapshot";
import { describeMetrics } from "../format";
import { recordCellRender } from "./TaskGrid.instrumentation";

/** Gap between cells, px. Kept small so 1000+ items still read as a wave. */
const CELL_GAP = 2;

export interface TaskGridProps {
  tasks: TaskState[];
}

export function TaskGrid({ tasks }: TaskGridProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const { width, height } = useContainerSize(containerRef);
  const layout = useMemo(
    () => bestGridLayout(tasks.length, width, height, CELL_GAP),
    [tasks.length, width, height],
  );

  return (
    <section
      aria-label="task grid"
      data-testid={tasks.length === 0 ? "task-grid-empty" : "task-grid"}
      className="min-h-0 rounded-lg border border-border bg-bg-panel p-3"
    >
      <div ref={containerRef} className="h-full w-full">
        {tasks.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-text-muted">
            Waiting for work items…
          </div>
        ) : (
          <div
            role="grid"
            aria-rowcount={layout.rows}
            aria-colcount={layout.cols}
            className="grid h-full w-full"
            style={{
              gap: `${CELL_GAP}px`,
              gridTemplateColumns: `repeat(${layout.cols}, minmax(0, 1fr))`,
              gridTemplateRows: `repeat(${layout.rows}, minmax(0, 1fr))`,
            }}
          >
            {tasks.map((task) => (
              <Cell
                key={task.task_id}
                taskId={task.task_id}
                text={task.task_text}
                status={task.status}
                folder={task.folder}
                backend={task.agent_backend}
                metrics={describeMetrics(task.data)}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * Pick the column count that maximizes cell size for `n` items in a
 * `w`×`h` box with `gap` px between cells. Cells render as `1fr`×`1fr`
 * so the chosen layout fills the panel completely.
 */
function bestGridLayout(
  n: number,
  w: number,
  h: number,
  gap: number,
): { cols: number; rows: number } {
  if (n <= 0) return { cols: 1, rows: 1 };
  if (w <= 0 || h <= 0) return { cols: 1, rows: n };

  let best = { cols: 1, rows: n };
  let bestSize = 0;
  for (let cols = 1; cols <= n; cols++) {
    const rows = Math.ceil(n / cols);
    const cellW = (w - (cols - 1) * gap) / cols;
    const cellH = (h - (rows - 1) * gap) / rows;
    const size = Math.min(cellW, cellH);
    if (size > bestSize) {
      bestSize = size;
      best = { cols, rows };
    }
  }
  return best;
}

function useContainerSize(ref: RefObject<HTMLDivElement | null>): {
  width: number;
  height: number;
} {
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const el = ref.current;
    // jsdom has no ResizeObserver; the grid then falls back to a single
    // column, which is fine for tests that only assert cell presence.
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) setSize({ width: rect.width, height: rect.height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);

  return size;
}

interface CellProps {
  taskId: string;
  text: string;
  status: TaskStatus;
  folder: string;
  backend: string;
  metrics: string;
}

const Cell = memo(function Cell({
  taskId,
  text,
  status,
  folder,
  backend,
  metrics,
}: CellProps) {
  recordCellRender();
  const className = `rounded-sm ${statusClasses(status)}`;
  return (
    <div
      role="gridcell"
      data-testid={`task-cell-${taskId}`}
      data-status={status}
      title={tooltip({ taskId, text, status, folder, backend, metrics })}
      className={className}
    />
  );
});

function statusClasses(status: TaskStatus): string {
  switch (status) {
    case "complete":
      return "bg-status-complete";
    case "started":
    case "working":
      // `animate-pulse` keeps the eye drawn to the active wavefront.
      return "bg-status-working animate-pulse";
    case "failed":
      // A failed attempt returns the task to the pool (its checkbox stays
      // unticked), so it renders as unclaimed until the next claim.
      // falls through
    case "pending":
    default:
      return "bg-status-idle";
  }
}

/** Viewer-friendly status wording for the hover tooltip. */
function statusLabel(status: TaskStatus): string {
  switch (status) {
    case "started":
    case "working":
      return "running";
    case "complete":
      return "succeeded";
    case "failed":
      return "pending (last attempt failed)";
    case "pending":
    default:
      return "pending";
  }
}

function tooltip(props: CellProps): string {
  const { text, status, folder, backend, metrics } = props;
  const lines: string[] = [text];
  lines.push(`status: ${statusLabel(status)}`);
  lines.push(`folder: ${folder}`);
  if (backend) lines.push(`agent: ${backend}`);
  if (metrics) lines.push(metrics);
  return lines.join("\n");
}
