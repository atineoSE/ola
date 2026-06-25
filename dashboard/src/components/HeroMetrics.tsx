/*
 * Hero Metrics Strip — the big numbers at the top of the dashboard.
 *
 * Four tiles (a fifth appears when a progress probe is configured), each
 * folding related figures onto one card so the strip stays compact:
 *   - Tasks    — completed / total big, with the failed count and a
 *                completion bar beneath.
 *   - Agents   — the live concurrency control (actual / target + stepper).
 *   - Output tok/sec — the live fleet rate, with durable avg / max beneath.
 *   - Elapsed  — the active-time stopwatch, with the running output-token
 *                total (millions) beneath.
 *
 * Layout target: projector-friendly, a single row on wide screens, wrapping
 * on narrower ones. Each tile is self-contained so the grid can be reordered
 * or partially hidden without leaking state between tiles.
 */

import type { Counters, ProgressMetric } from "../snapshot";
import { formatElapsed, formatMillions, formatTokensPerSec } from "../format";
import { useActiveElapsed } from "../hooks/useActiveElapsed";
import { Sparkline } from "./Sparkline";

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
  /** Live fleet output throughput: the windowed current rate, held across
   * reporting gaps while agents are active. `null` when the run is idle or
   * finished — there is no live rate to show, so the tile reads `—`. */
  liveTokensPerSec?: number | null;
  /** Peak single-agent throughput across the run, derived from the durable
   * per-task metrics so it persists after the run ends. `null` before any
   * task reports usable metrics. */
  peakTokensPerSec?: number | null;
  /** Per-task decode-weighted average throughput (`Σ tokens / Σ decode-secs`),
   * the headline for a non-streaming backend. It is a weighted mean of the
   * per-task rates, so it stays ≤ `peakTokensPerSec` — the right partner for the
   * peak, unlike the fleet wall-clock average which parallelism can push above
   * the peak. `null` before any task reports usable metrics. */
  avgTaskTokensPerSec?: number | null;
  /** Total output tokens generated across the run so far, displayed in
   * millions so the running total can be watched climbing. Also the numerator
   * for the durable average tok/sec (over the active-elapsed clock). */
  totalOutputTokens?: number;
  /** Whether the run's backend emits a live, token-level streaming rate. When
   * `false` (the `oh`/`ct` backends, which recover token counts only post-hoc),
   * the tok/sec tile shows the durable **average** as its headline with the peak
   * beneath, instead of a permanently-blank live rate. Defaults to `true` so a
   * streaming backend (`cc`/`cx`) keeps the live headline. */
  liveRateSupported?: boolean;
  /** Task-defined progress probes for the folder, keyed by metric name. The
   * primary (first) metric renders as an extra tile with a sparkline; absent or
   * empty renders nothing, leaving the no-probe layout unchanged. */
  progress?: Record<string, ProgressMetric>;
}

export function HeroMetrics({
  counters,
  activeElapsedSeconds = null,
  activeAnchorTs = null,
  agentsTarget = null,
  onAgentsTargetChange,
  liveTokensPerSec = null,
  peakTokensPerSec = null,
  avgTaskTokensPerSec = null,
  totalOutputTokens = 0,
  liveRateSupported = true,
  progress,
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
  // Durable lifetime average: total output tokens over the run's active wall
  // time. Both come from the files, so unlike the live rate this stays put
  // when the run finishes and survives a page reload. `null` until work lands.
  const avgTokensPerSec =
    elapsedSeconds != null && elapsedSeconds > 0 && totalOutputTokens > 0
      ? totalOutputTokens / elapsedSeconds
      : null;
  // The primary progress probe is the first entry the task published. Absent or
  // empty leaves the strip exactly as it was — no extra tile, no layout shift.
  const primaryProgress = progress ? Object.entries(progress)[0] : undefined;
  // The folded strip is four tiles, or five when a progress probe is present —
  // size the wide-screen grid so it fills one row either way.
  const xlCols = primaryProgress ? "xl:grid-cols-5" : "xl:grid-cols-4";
  // A non-zero failure count earns the alarm colour; zero stays muted so a
  // clean run doesn't draw the eye to a red 0.
  const failedTone = failed > 0 ? "text-status-failed" : "text-text-muted";

  return (
    <section
      aria-label="hero metrics"
      className={`grid grid-cols-2 gap-4 md:grid-cols-3 ${xlCols}`}
    >
      <MetricTile
        label="Tasks"
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
          <div className="flex flex-col gap-3">
            {/* Failed attempts return their task to the pool, so they are not
                finished work — shown beneath the total as a secondary figure. */}
            <div className="font-mono text-2xl tabular-nums">
              <span data-testid="failed-value" className={failedTone}>
                {failed.toLocaleString()}
              </span>
              <span className="text-text-muted"> failed</span>
            </div>
            <ProgressBar
              pct={completionPct}
              ariaLabel={`${completed} of ${total_tasks} tasks completed`}
            />
          </div>
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

      {liveRateSupported ? (
        <MetricTile
          label="Output tok/sec"
          value={
            <span
              className="font-mono tabular-nums tracking-tight"
              data-testid="output-tokens-value"
            >
              {formatTokensPerSec(liveTokensPerSec)}
            </span>
          }
          footer={
            // Half the main value's font size (text-5xl → text-2xl), so avg/peak
            // read as a clearly secondary readout beneath the live rate. Both are
            // file-derived, so they persist after the run finishes (when the live
            // rate above falls back to `—`).
            <div className="flex justify-between font-mono text-2xl tabular-nums text-text-muted">
              <span data-testid="output-tokens-avg">
                avg {formatTokensPerSec(avgTokensPerSec)}
              </span>
              <span data-testid="output-tokens-max">
                max {formatTokensPerSec(peakTokensPerSec)}
              </span>
            </div>
          }
        />
      ) : (
        // No live token stream (oh / ct recover counts only post-hoc), so a
        // windowed live rate is never available — a permanent "—" would read as
        // broken. Show the durable average as the headline instead, with the
        // peak as the secondary readout. Both are file-derived and per-task
        // scoped (decode-weighted avg ≤ peak), so the headline never reads above
        // "max" — unlike the fleet wall-clock average used in the live layout.
        <MetricTile
          label="Avg tok/sec"
          value={
            <span
              className="font-mono tabular-nums tracking-tight"
              data-testid="output-tokens-value"
            >
              {formatTokensPerSec(avgTaskTokensPerSec)}
            </span>
          }
          footer={
            <div className="font-mono text-2xl tabular-nums text-text-muted">
              <span data-testid="output-tokens-max">
                max {formatTokensPerSec(peakTokensPerSec)}
              </span>
            </div>
          }
        />
      )}

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
        footer={
          // The running output-token total folded beneath the stopwatch: same
          // accent as before, half the headline size so it reads as secondary.
          <div className="font-mono text-2xl tabular-nums text-accent">
            <span data-testid="total-output-tokens-value">
              {formatMillions(totalOutputTokens)}
            </span>
            <span className="text-text-muted"> M output tokens</span>
          </div>
        }
      />

      {primaryProgress && (
        <MetricTile
          label={primaryProgress[0]}
          value={
            <span
              className="font-mono tabular-nums tracking-tight"
              data-testid="progress-value"
            >
              {primaryProgress[1].value.toLocaleString()}
            </span>
          }
          footer={
            <Sparkline
              data-testid="progress-sparkline"
              className="h-6 w-full text-accent"
              values={primaryProgress[1].series.map(([, v]) => v)}
            />
          }
        />
      )}
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
