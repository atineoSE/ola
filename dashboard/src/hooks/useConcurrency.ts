/*
 * Live concurrency target for one folder, through the ola-dashboard server's
 * control plane.
 *
 * The cap is owned by the scheduler as `<folder>/.ola/concurrency` (Ola
 * format, re-read every dispatch tick). The browser can't touch that file, so
 * this hook proxies the server's same-origin endpoints: `GET
 * /api/concurrency?folder=` on mount and on folder switch, debounced `PUT
 * /api/concurrency` when `setTarget` is called.
 *
 * A `null` target means "no file yet": the scheduler runs at Ola's default cap
 * (2) until either the first scheduler tick materializes the file or the first
 * `setTarget` creates it. `0` pauses new agent starts (in-flight agents finish).
 */

import { useEffect, useRef, useState } from "react";

/** Debounce for writes while the target is stepped repeatedly. */
const PUT_DEBOUNCE_MS = 250;

/** The loaded target, tagged with the folder it belongs to so a folder switch
 * reports unavailable until the new GET lands. */
interface Loaded {
  folder: string;
  value: number | null;
}

export interface UseConcurrencyResult {
  /** Current target; `null` = no concurrency file yet (default applies). */
  target: number | null;
  /** False until the GET for the current folder succeeds. */
  available: boolean;
  /** Optimistically set the target and (debounced) write it through. */
  setTarget: (next: number) => void;
}

export interface UseConcurrencyOptions {
  /** Override `globalThis.fetch` — exposed for tests. */
  fetchImpl?: typeof fetch;
}

export function useConcurrency(
  folder: string | null,
  options: UseConcurrencyOptions = {},
): UseConcurrencyResult {
  const { fetchImpl } = options;
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!folder) return;
    const doFetch = fetchImpl ?? globalThis.fetch;
    let cancelled = false;
    doFetch(`/api/concurrency?folder=${encodeURIComponent(folder)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((body: { concurrency: number | null }) => {
        if (!cancelled) setLoaded({ folder, value: body.concurrency });
      })
      .catch(() => {
        if (!cancelled) setLoaded(null);
      });
    return () => {
      cancelled = true;
    };
  }, [folder, fetchImpl]);

  useEffect(() => {
    return () => {
      if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    };
  }, []);

  const available =
    folder !== null && loaded !== null && loaded.folder === folder;

  const setTarget = (next: number) => {
    if (!folder) return;
    const doFetch = fetchImpl ?? globalThis.fetch;
    setLoaded({ folder, value: next });
    if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      void doFetch("/api/concurrency", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder, concurrency: next }),
      }).catch(() => {
        // Fire-and-forget; re-syncs from GET on the next folder switch.
      });
    }, PUT_DEBOUNCE_MS);
  };

  return { target: available ? loaded.value : null, available, setTarget };
}
