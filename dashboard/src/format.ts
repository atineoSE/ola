/*
 * Display formatters shared across dashboard widgets. Kept React-free and
 * pure so they can be unit-tested in isolation.
 */

import type { Metrics } from "./snapshot/types";

/**
 * Format an elapsed duration as `mm:ss`. Durations over 60 minutes overflow
 * the minutes field (e.g. `120:05`) rather than rolling into hours — the
 * demo's expected ceiling is ~30 minutes, and a two-digit-minutes display
 * stays readable on the projector even past the design target.
 *
 * Negative or NaN inputs render as `--:--`, signalling "not started yet".
 */
export function formatElapsed(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) {
    return "--:--";
  }
  const total = Math.floor(seconds);
  const mm = Math.floor(total / 60);
  const ss = total % 60;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

/** Format a tokens/sec rate for display, e.g. `46.0`. `—` when absent. */
export function formatTokensPerSec(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(1);
}

/**
 * Format a raw token count as millions, e.g. `1.23` for 1,230,000. Two decimals
 * keep the readout responsive to growth (each 0.01 is 10k tokens) while staying
 * compact. `0.00` for an empty/absent count, never a placeholder — the tile is
 * a running total that starts at zero and only climbs.
 */
export function formatMillions(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return "0.00";
  return (value / 1_000_000).toFixed(2);
}

/**
 * Extract the typed `Metrics` block from an event's `data` payload, or
 * `null` when the event carries no usable metrics (e.g. a `started` event,
 * or a backend that can't report usage). A block is usable only when
 * `tokens_per_sec` is a finite number.
 */
export function readMetrics(
  data: Record<string, unknown> | null | undefined,
): Metrics | null {
  if (data == null) return null;
  const raw = data.metrics;
  if (raw == null || typeof raw !== "object") return null;
  const m = raw as Record<string, unknown>;
  const tps = m.tokens_per_sec;
  if (typeof tps !== "number" || !Number.isFinite(tps)) return null;
  return {
    output_tokens: typeof m.output_tokens === "number" ? m.output_tokens : 0,
    decode_ms: typeof m.decode_ms === "number" ? m.decode_ms : 0,
    tokens_per_sec: tps,
  };
}

/**
 * A single displayable metric pulled from an event's `data.metrics` block.
 */
export interface MetricEntry {
  key: string;
  value: string;
}

/**
 * Turn an event's `data.metrics` block into an ordered list of displayable
 * key/value metrics (throughput then volume). Empty when no metrics block
 * is present.
 */
export function metricEntries(
  data: Record<string, unknown> | null | undefined,
): MetricEntry[] {
  const m = readMetrics(data);
  if (m == null) return [];
  return [
    { key: "tok/s", value: m.tokens_per_sec.toFixed(1) },
    { key: "tokens", value: String(m.output_tokens) },
  ];
}

/**
 * Compact one-line summary of an event's metrics, e.g.
 * `tok/s: 46.0 · tokens: 487`. Empty string when there are no metrics.
 */
export function describeMetrics(
  data: Record<string, unknown> | null | undefined,
): string {
  return metricEntries(data)
    .map((m) => `${m.key}: ${m.value}`)
    .join(" · ");
}
