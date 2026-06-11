/*
 * React hook that subscribes the dashboard to `{collectorUrl}/stream` and
 * exposes a single, always-fresh `CollectorStore` derived from the
 * server's opening `snapshot` plus subsequent lifecycle `event` messages.
 *
 * The hook is the dashboard's only network seam — pages render off the
 * returned `store` and ignore the transport entirely.
 */

import { useEffect, useReducer } from "react";

import {
  EMPTY_STORE,
  applyEvent,
  applyManifest,
  fromSnapshot,
  type CollectorStore,
} from "./store";
import type { LifecycleEvent, ManifestMessage, Snapshot } from "./types";

export type ConnectionStatus = "idle" | "connecting" | "open" | "closed";

/**
 * Raw EventSource lifecycle state. `null` means "not yet open" — combined
 * with the caller's `collectorUrl` it resolves to either `idle` (no URL)
 * or `connecting` (URL given, not yet opened).
 */
type ConnectionPhase = "open" | "closed" | null;

interface InternalState {
  store: CollectorStore;
  conn: ConnectionPhase;
}

const INITIAL: InternalState = { store: EMPTY_STORE, conn: null };

type Action =
  | { type: "snapshot"; snapshot: Snapshot }
  | { type: "event"; event: LifecycleEvent }
  | { type: "manifest"; manifest: ManifestMessage }
  | { type: "reset" }
  | { type: "open" }
  | { type: "error" };

function reducer(state: InternalState, action: Action): InternalState {
  switch (action.type) {
    case "snapshot":
      return { ...state, store: fromSnapshot(action.snapshot) };
    case "event":
      return { ...state, store: applyEvent(state.store, action.event) };
    case "manifest":
      return { ...state, store: applyManifest(state.store, action.manifest) };
    case "reset":
      return INITIAL;
    case "open":
      return { ...state, conn: "open" };
    case "error":
      return { ...state, conn: "closed" };
  }
}

function resolveStatus(
  collectorUrl: string | null,
  conn: ConnectionPhase,
): ConnectionStatus {
  if (collectorUrl == null) return "idle";
  if (conn === "open") return "open";
  if (conn === "closed") return "closed";
  return "connecting";
}

export interface UseCollectorStreamOptions {
  /** Override `globalThis.EventSource` — exposed for tests. */
  eventSourceCtor?: typeof EventSource;
}

export interface UseCollectorStreamResult {
  store: CollectorStore;
  status: ConnectionStatus;
}

function buildStreamUrl(collectorUrl: string): string {
  // `new URL` against a base lets callers pass either an origin
  // (`http://localhost:8000`) or a base path
  // (`https://collector.example.com/api/`).
  return new URL("stream", ensureTrailingSlash(collectorUrl)).toString();
}

function ensureTrailingSlash(url: string): string {
  return url.endsWith("/") ? url : `${url}/`;
}

export function useCollectorStream(
  collectorUrl: string | null,
  options: UseCollectorStreamOptions = {},
): UseCollectorStreamResult {
  const [state, dispatch] = useReducer(reducer, INITIAL);

  useEffect(() => {
    if (!collectorUrl) {
      return;
    }

    const Ctor = options.eventSourceCtor ?? globalThis.EventSource;
    if (!Ctor) {
      // EventSource is missing (SSR, or a stripped-down jsdom). Surface
      // as `closed` instead of crashing.
      dispatch({ type: "error" });
      return;
    }

    dispatch({ type: "reset" });

    const es = new Ctor(buildStreamUrl(collectorUrl));

    const onOpen = () => dispatch({ type: "open" });
    const onError = () => dispatch({ type: "error" });
    const onSnapshot = (ev: MessageEvent) => {
      dispatch({ type: "snapshot", snapshot: JSON.parse(ev.data) as Snapshot });
    };
    const onEvent = (ev: MessageEvent) => {
      dispatch({
        type: "event",
        event: JSON.parse(ev.data) as LifecycleEvent,
      });
    };
    const onManifest = (ev: MessageEvent) => {
      dispatch({
        type: "manifest",
        manifest: JSON.parse(ev.data) as ManifestMessage,
      });
    };

    es.addEventListener("open", onOpen);
    es.addEventListener("error", onError);
    es.addEventListener("snapshot", onSnapshot as EventListener);
    es.addEventListener("event", onEvent as EventListener);
    es.addEventListener("manifest", onManifest as EventListener);

    return () => {
      es.removeEventListener("open", onOpen);
      es.removeEventListener("error", onError);
      es.removeEventListener("snapshot", onSnapshot as EventListener);
      es.removeEventListener("event", onEvent as EventListener);
      es.removeEventListener("manifest", onManifest as EventListener);
      es.close();
    };
  }, [collectorUrl, options.eventSourceCtor]);

  return {
    store: state.store,
    status: resolveStatus(collectorUrl, state.conn),
  };
}
