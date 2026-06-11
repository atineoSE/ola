/*
 * Hero Metrics Strip — the four big numbers at the top of the dashboard.
 *
 * Layout target: projector-friendly, single row on wide screens, wraps
 * to a 2x2 grid on narrower ones. Each tile is self-contained so the
 * grid can later be reordered or partially hidden without leaking state
 * between tiles.
 *
 * v2 dropped the verifier deltas, so the "errors remaining" tile is gone;
 * the tiles now reflect task lifecycle counts (completed / failed / active)
 * with task-published metrics surfaced separately in the Metrics panel.
 */

import type { Counters } from "../snapshot";
import type { OutputTokensPerSec } from "../hooks/useOutputTokensPerSec";
import { formatElapsed, formatMillions, formatTokensPerSec } from "../format";
import { useActiveElapsed } from "../hooks/useActiveElapsed";

export interface HeroMetricsProps {
  counters: Counters;
  /** Accumulated active wall seconds (the stopwatch base); `null`/absent until
   * any agent has run. Idle gaps are already excluded server-side. */
  activeElapsedSeconds?: number | null;
  /** Ts to tick the still-running tail from; `null`/absent = idle, freeze the
   * elapsed readout. */
  activeAnchorTs?: string | null;
  /** Concurrency target (`null` = no file yet, orchestrator default). */
  agentsTarget?: number | null;
  /** Step the target; absent when the collector has no path registered —
   * the agents tile then shows the actual count only. */
  onAgentsTargetChange?: (next: number) => void;
  /** Live fleet output throughput: current rate (held across gaps) plus the
   * run's average and peak. `null` fields before the first reading lands. */
  outputTokensPerSec?: OutputTokensPerSec | null;
  /** Total output tokens generated across the run so far, displayed in
   * millions so the running total can be watched climbing. */
  totalOutputTokens?: number;
}

export function HeroMetrics({
  counters,
  activeElapsedSeconds = null,
  activeAnchorTs = null,
  agentsTarget = null,
  onAgentsTargetChange,
  outputTokensPerSec = null,
  totalOutputTokens = 0,
}: HeroMetricsProps) {
  const { total_tasks, completed, failed, active } = counters;
  // The elapsed tile is a stopwatch that only advances while ≥1 agent is
  // active: the server hands back the accumulated active seconds, and the hook
  // ticks the open tail (anchor set) or freezes it (anchor null) when idle.
  // `null` base before any agent has run renders as the --:-- not-started mark.
  const started = activeAnchorTs != null || (activeElapsedSeconds ?? 0) > 0;
  const elapsedSeconds = useActiveElapsed(
    started ? (activeElapsedSeconds ?? 0) : null,
    activeAnchorTs,
  );
  // Avoid a divide-by-zero blip before the snapshot lands.
  const completionPct =
    total_tasks > 0 ? Math.min(100, (completed / total_tasks) * 100) : 0;

  return (
    <section
      aria-label="hero metrics"
      className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6"
    >
      <MetricTile
        label="Tasks completed"
        value={
          <span
            className="font-mono tabular-nums tracking-tight"
            data-testid="tasks-completed-value"
          >
            {completed.toLocaleString()}
            <span className="text-text-muted">
              {" / "}
              {total_tasks.toLocaleString()}
            </span>
          </span>
        }
        footer={
          <ProgressBar
            pct={completionPct}
            ariaLabel={`${completed} of ${total_tasks} tasks completed`}
          />
        }
      />

      {onAgentsTargetChange ? (
        <AgentsTile
          active={active}
          target={agentsTarget}
          onTargetChange={onAgentsTargetChange}
        />
      ) : (
        <MetricTile
          label="Active agents"
          value={
            <span
              className="font-mono tabular-nums tracking-tight"
              data-testid="active-agents-value"
            >
              {active.toLocaleString()}
            </span>
          }
        />
      )}

      <MetricTile
        label="Output tok/sec"
        value={
          <span
            className="font-mono tabular-nums tracking-tight"
            data-testid="output-tokens-value"
          >
            {formatTokensPerSec(outputTokensPerSec?.current ?? null)}
          </span>
        }
        footer={
          // Half the main value's font size (text-5xl → text-2xl), so avg/peak
          // read as a clearly secondary readout beneath the live rate.
          <div className="flex justify-between font-mono text-2xl tabular-nums text-text-muted">
            <span data-testid="output-tokens-avg">
              avg {formatTokensPerSec(outputTokensPerSec?.avg ?? null)}
            </span>
            <span data-testid="output-tokens-max">
              max {formatTokensPerSec(outputTokensPerSec?.max ?? null)}
            </span>
          </div>
        }
      />

      <MetricTile
        label="Elapsed"
        value={
          <span
            className="font-mono tabular-nums tracking-tight"
            data-testid="elapsed-value"
          >
            {formatElapsed(elapsedSeconds)}
          </span>
        }
      />

      <MetricTile
        label="Failed"
        value={
          <span
            className="font-mono tabular-nums tracking-tight"
            data-testid="failed-value"
          >
            {failed.toLocaleString()}
          </span>
        }
      />

      <MetricTile
        label="Output tokens (M)"
        value={
          <span
            className="font-mono tabular-nums tracking-tight text-accent"
            data-testid="total-output-tokens-value"
          >
            {formatMillions(totalOutputTokens)}
          </span>
        }
      />
    </section>
  );
}

interface AgentsTileProps {
  active: number;
  target: number | null;
  onTargetChange: (next: number) => void;
}

/**
 * Agents tile with the live concurrency control: "actual / target" plus a
 * +/- stepper for fine-grained adjustment of the target. When no target
 * file exists yet (`target === null`) the first step anchors on the actual
 * count, so +/- means "one more/fewer than what's running now".
 */
function AgentsTile({ active, target, onTargetChange }: AgentsTileProps) {
  const step = (delta: number) => {
    const anchor = target ?? active;
    onTargetChange(Math.max(0, anchor + delta));
  };

  return (
    <div className="rounded-lg border border-border bg-bg-panel px-6 py-5 flex flex-col gap-3">
      <div className="text-sm uppercase tracking-wider text-text-muted">
        Agents
      </div>
      <div className="flex items-center justify-between gap-3">
        {/* whitespace-nowrap keeps "actual / target" on one line; the font
            steps down on the tight 4-column layout instead of wrapping. */}
        <div className="whitespace-nowrap text-4xl xl:text-5xl font-semibold leading-none font-mono tabular-nums tracking-tight">
          <span data-testid="active-agents-value">{active.toLocaleString()}</span>
          <span className="text-text-muted">
            {" / "}
            <span data-testid="agents-target-value">
              {target === null ? "–" : target.toLocaleString()}
            </span>
          </span>
        </div>
        <div className="flex shrink-0 flex-col gap-1">
          <StepButton
            label="Increase target"
            testId="agents-target-up"
            onClick={() => step(1)}
          >
            +
          </StepButton>
          <StepButton
            label="Decrease target"
            testId="agents-target-down"
            onClick={() => step(-1)}
          >
            −
          </StepButton>
        </div>
      </div>
      <div className="flex justify-between text-xs uppercase tracking-wider text-text-muted">
        <span>Actual</span>
        <span>Target{target === 0 ? " (paused)" : ""}</span>
      </div>
    </div>
  );
}

function StepButton({
  label,
  testId,
  onClick,
  children,
}: {
  label: string;
  testId: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      data-testid={testId}
      onClick={onClick}
      className="h-7 w-7 rounded border border-border bg-bg text-base leading-none text-text hover:border-accent"
    >
      {children}
    </button>
  );
}

interface MetricTileProps {
  label: string;
  value: React.ReactNode;
  footer?: React.ReactNode;
}

function MetricTile({ label, value, footer }: MetricTileProps) {
  return (
    <div className="rounded-lg border border-border bg-bg-panel px-6 py-5 flex flex-col gap-3">
      <div className="text-sm uppercase tracking-wider text-text-muted">
        {label}
      </div>
      <div className="text-5xl font-semibold leading-none">{value}</div>
      {footer}
    </div>
  );
}

interface ProgressBarProps {
  pct: number;
  ariaLabel: string;
}

function ProgressBar({ pct, ariaLabel }: ProgressBarProps) {
  return (
    <div
      role="progressbar"
      aria-label={ariaLabel}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(pct)}
      className="h-2 w-full rounded-full bg-border overflow-hidden"
    >
      <div
        data-testid="progress-bar-fill"
        className="h-full bg-status-complete transition-[width] duration-500 ease-out"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
