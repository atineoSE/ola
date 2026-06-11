/*
 * React hook that polls the `ola-dashboard` server's `/api/snapshot` endpoint
 * and exposes the latest `Snapshot` plus a coarse connection status.
 *
 * The server is stateless: every request re-parses the agent folder, so each
 * poll returns the full current state and simply replaces the previous one —
 * there is no incremental event folding and nothing to reconcile. The hook is
 * the dashboard's only network seam; pages render off the returned `snapshot`
 * and ignore the transport entirely.
 */

import { useEffect, useState } from "react";

import { EMPTY_SNAPSHOT } from "./store";
import type { Snapshot } from "./types";

export type ConnectionStatus = "connecting" | "open" | "closed";

/** Default poll cadence. Matches the agent-folder churn rate well enough for
 * a live wall display without hammering the filesystem. */
export const DEFAULT_REFRESH_MS = 1500;

export interface UseSnapshotOptions {
  /** Poll interval in milliseconds. */
  refreshMs?: number;
  /** Snapshot endpoint (default `/api/snapshot`, same origin as the SPA). */
  url?: string;
  /** Override `globalThis.fetch` — exposed for tests. */
  fetchImpl?: typeof fetch;
}

export interface UseSnapshotResult {
  snapshot: Snapshot;
  status: ConnectionStatus;
}

export function useSnapshot(
  options: UseSnapshotOptions = {},
): UseSnapshotResult {
  const {
    refreshMs = DEFAULT_REFRESH_MS,
    url = "/api/snapshot",
    fetchImpl,
  } = options;

  const [snapshot, setSnapshot] = useState<Snapshot>(EMPTY_SNAPSHOT);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        // Resolved per-call so a missing `fetch` (SSR / stripped jsdom) lands
        // in the catch below as a `closed` poll rather than crashing.
        const doFetch = fetchImpl ?? globalThis.fetch;
        const res = await doFetch(url);
        if (!res.ok) throw new Error(`${res.status}`);
        const next = (await res.json()) as Snapshot;
        if (!cancelled) {
          setSnapshot(next);
          setStatus("open");
        }
      } catch {
        // Server down / restarting / transient error. Keep the last snapshot
        // on screen (files are truth — nothing is lost) and flag the gap; the
        // next tick recovers on its own.
        if (!cancelled) setStatus("closed");
      }
    };

    void poll();
    const id = setInterval(() => void poll(), refreshMs);

    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [refreshMs, url, fetchImpl]);

  return { snapshot, status };
}
