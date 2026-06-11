/*
 * Live concurrency target for one project, through the collector's
 * control plane.
 *
 * The cap is owned by the orchestrator host as `<plan folder>/.ola/concurrency`
 * (Ola format, re-read every dispatch tick). The browser can't touch that
 * file, so this hook proxies: `GET /concurrency?folder=` on mount and on
 * project switch, debounced `POST /concurrency` when `setTarget` is called.
 *
 * `available` is false when the collector has no plan-folder path
 * registered for the project (404) — there is then no file to control.
 * A `null` target means "no file yet": the orchestrator runs at Ola's
 * default cap of 1 until the first `setTarget` creates the file. 0 pauses
 * new agent starts (in-flight agents finish).
 */

import { useEffect, useRef, useState } from "react";

/** Debounce for POSTs while the target is stepped repeatedly. */
const POST_DEBOUNCE_MS = 250;

/** The loaded target, tagged with the (collector, project) it belongs to so
 * a project switch reports unavailable until the new GET lands. */
interface Loaded {
  key: string;
  value: number | null;
}

export interface UseConcurrencyResult {
  /** Current target; `null` = no concurrency file yet (default applies). */
  target: number | null;
  /** False until the GET for the current (collector, project) succeeds. */
  available: boolean;
  /** Optimistically set the target and (debounced) write it through. */
  setTarget: (next: number) => void;
}

function base(collectorUrl: string): string {
  return collectorUrl.endsWith("/") ? collectorUrl.slice(0, -1) : collectorUrl;
}

export function useConcurrency(
  collectorUrl: string | null,
  folder: string | null,
): UseConcurrencyResult {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const key = collectorUrl && folder ? `${collectorUrl}|${folder}` : null;

  useEffect(() => {
    if (!key || !collectorUrl || !folder) return;
    let cancelled = false;
    fetch(`${base(collectorUrl)}/concurrency?folder=${encodeURIComponent(folder)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((body: { concurrency: number | null }) => {
        if (!cancelled) setLoaded({ key, value: body.concurrency });
      })
      .catch(() => {
        // 404 (no registered path) or network trouble: nothing to control.
        if (!cancelled) setLoaded(null);
      });
    return () => {
      cancelled = true;
    };
  }, [key, collectorUrl, folder]);

  useEffect(() => {
    return () => {
      if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    };
  }, []);

  const available = key !== null && loaded !== null && loaded.key === key;

  const setTarget = (next: number) => {
    if (!key || !collectorUrl || !folder) return;
    setLoaded({ key, value: next });
    if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      void fetch(`${base(collectorUrl)}/concurrency`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder, concurrency: next }),
      }).catch(() => {
        // Fire-and-forget like the rest of the demo plumbing; the value
        // re-syncs from GET on the next project switch.
      });
    }, POST_DEBOUNCE_MS);
  };

  return { target: available ? loaded.value : null, available, setTarget };
}
