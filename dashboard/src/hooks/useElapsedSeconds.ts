/*
 * Tick a wall-clock-elapsed counter once per second, anchored to the
 * timestamp the collector reports as the first `started` event.
 *
 * When `stopTs` is given (the last terminal event of a finished run) the
 * counter freezes at `stopTs - firstStartedTs` and the interval is torn
 * down — the run is over, there is nothing left to tick for.
 *
 * Why a hook instead of computing on every render: the collector store
 * does not push updates between events, so a long quiet period would
 * freeze the elapsed-time readout if it were derived only from props.
 */

import { useEffect, useState } from "react";

/** `null` until the first `started` event has been observed. */
export function useElapsedSeconds(
  firstStartedTs: string | null,
  /** Freeze the readout at this timestamp (run finished). `null` = keep ticking. */
  stopTs: string | null = null,
  /** Override `Date.now()` for deterministic tests. */
  now: () => number = Date.now,
): number | null {
  const [nowMs, setNowMs] = useState(() => now());
  // Re-anchor immediately when `firstStartedTs` flips (e.g. null → string,
  // or after a `POST /reset`). Computing in-render with a paired setState
  // is React's documented "derived state" escape hatch — it avoids the
  // 1-second blank flash you'd get if we waited for the first interval
  // tick, without the cascading-render warning that setState-in-effect
  // would trigger.
  const [anchor, setAnchor] = useState(firstStartedTs);
  if (firstStartedTs !== anchor) {
    setAnchor(firstStartedTs);
    setNowMs(now());
  }

  const running = firstStartedTs != null && stopTs == null;

  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNowMs(now()), 1000);
    return () => clearInterval(id);
  }, [running, now]);

  if (firstStartedTs != null && stopTs != null) {
    const stopMs = Date.parse(stopTs);
    if (!Number.isNaN(stopMs)) {
      return computeElapsed(firstStartedTs, stopMs);
    }
  }
  return computeElapsed(firstStartedTs, nowMs);
}

function computeElapsed(
  firstStartedTs: string | null,
  nowMs: number,
): number | null {
  if (firstStartedTs == null) return null;
  const startMs = Date.parse(firstStartedTs);
  if (Number.isNaN(startMs)) return null;
  return Math.max(0, (nowMs - startMs) / 1000);
}
