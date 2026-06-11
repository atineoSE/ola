/*
 * Active-time stopwatch for the Elapsed hero tile.
 *
 * Unlike a plain wall clock, this counts only the time ≥1 agent is actually
 * running: the server (`build_snapshot`) accumulates the active wall seconds
 * from the event stream (idle gaps excluded) and hands back `baseSeconds` plus,
 * when an agent is still running, the `anchorTs` of the last event so the open
 * tail can be ticked out to "now". When `anchorTs` is null the run is idle and
 * the readout freezes at `baseSeconds`.
 *
 * Deriving the base server-side keeps the dashboard stateless and refresh-safe
 * (the files are the source of truth): a reload re-reads the same accumulated
 * value rather than restarting a client-side counter.
 */

import { useEffect, useState } from "react";

export function useActiveElapsed(
  /** Accumulated active seconds so far; `null` until any agent has run. */
  baseSeconds: number | null,
  /** Tick anchor while an agent is running; `null` = idle, freeze the readout. */
  anchorTs: string | null,
  /** Override `Date.now()` for deterministic tests. */
  now: () => number = Date.now,
): number | null {
  const running = anchorTs != null;
  const [nowMs, setNowMs] = useState(() => now());

  // Re-sample the clock immediately whenever the running tail's anchor moves
  // (a new event landed) so the tail doesn't lag a tick behind the poll.
  const [seenAnchor, setSeenAnchor] = useState(anchorTs);
  if (anchorTs !== seenAnchor) {
    setSeenAnchor(anchorTs);
    setNowMs(now());
  }

  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNowMs(now()), 1000);
    return () => clearInterval(id);
  }, [running, now]);

  if (baseSeconds == null) return null;
  if (!running) return baseSeconds;
  const anchorMs = Date.parse(anchorTs);
  if (Number.isNaN(anchorMs)) return baseSeconds;
  return baseSeconds + Math.max(0, (nowMs - anchorMs) / 1000);
}
