/*
 * Resolve the collector URL the dashboard should connect to and expose a
 * setter that persists the user's choice.
 *
 * Precedence on first render:
 *   1. `?collector=<url>` URL search param  (one-shot override; promoted
 *      to localStorage so a reload without the param still works)
 *   2. localStorage  (sticky across reloads)
 *   3. The compiled-in default  (sensible local-dev origin)
 *
 * `setUrl(next)` updates localStorage AND rewrites the search param so
 * the displayed URL is shareable and a refresh re-resolves to the same
 * collector.
 *
 * Storage and `window` access are gated on `typeof window` so the hook
 * survives SSR and stripped-down jsdom — falling back to the default in
 * those cases.
 */

import { useCallback, useState } from "react";

export const DEFAULT_COLLECTOR_URL = "http://localhost:8000";
const STORAGE_KEY = "parallel-refactor.collectorUrl";
const URL_PARAM = "collector";

function readFromLocalStorage(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private-mode browsers throw on `localStorage` access; treat as
    // "no persisted value" and continue with the default.
    return null;
  }
}

function readFromUrlParam(): string | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const value = params.get(URL_PARAM);
  return value && value.length > 0 ? value : null;
}

function writeToLocalStorage(url: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, url);
  } catch {
    // Persistence is best-effort — the in-memory value still drives the
    // current session.
  }
}

function writeToUrlParam(url: string): void {
  if (typeof window === "undefined") return;
  const loc = window.location;
  const params = new URLSearchParams(loc.search);
  if (url === DEFAULT_COLLECTOR_URL) {
    params.delete(URL_PARAM);
  } else {
    params.set(URL_PARAM, url);
  }
  const query = params.toString();
  const nextUrl = `${loc.pathname}${query ? `?${query}` : ""}${loc.hash}`;
  window.history.replaceState(window.history.state, "", nextUrl);
}

function resolveInitial(): string {
  const fromParam = readFromUrlParam();
  if (fromParam) {
    // Promote a one-shot URL override to the persisted store so the next
    // reload still hits the same collector.
    writeToLocalStorage(fromParam);
    return fromParam;
  }
  const fromStorage = readFromLocalStorage();
  if (fromStorage) return fromStorage;
  return DEFAULT_COLLECTOR_URL;
}

export interface UseCollectorUrlResult {
  url: string;
  setUrl: (next: string) => void;
}

export function useCollectorUrl(): UseCollectorUrlResult {
  const [url, setUrlState] = useState<string>(resolveInitial);

  const setUrl = useCallback((next: string) => {
    const trimmed = next.trim();
    if (trimmed.length === 0) return;
    setUrlState(trimmed);
    writeToLocalStorage(trimmed);
    writeToUrlParam(trimmed);
  }, []);

  return { url, setUrl };
}
