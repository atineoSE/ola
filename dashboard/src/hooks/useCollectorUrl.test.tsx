import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  DEFAULT_COLLECTOR_URL,
  useCollectorUrl,
} from "./useCollectorUrl";

const STORAGE_KEY = "parallel-refactor.collectorUrl";

beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("useCollectorUrl", () => {
  it("returns the default when no URL param or stored value is set", () => {
    const { result } = renderHook(() => useCollectorUrl());
    expect(result.current.url).toBe(DEFAULT_COLLECTOR_URL);
  });

  it("returns the stored value when localStorage has one", () => {
    window.localStorage.setItem(STORAGE_KEY, "http://stored.example/");
    const { result } = renderHook(() => useCollectorUrl());
    expect(result.current.url).toBe("http://stored.example/");
  });

  it("prefers the `?collector=` URL param over localStorage", () => {
    window.localStorage.setItem(STORAGE_KEY, "http://stored.example/");
    window.history.replaceState(
      null,
      "",
      "/?collector=http%3A%2F%2Fparam.example%2F",
    );
    const { result } = renderHook(() => useCollectorUrl());
    expect(result.current.url).toBe("http://param.example/");
  });

  it("promotes a URL-param override into localStorage so reloads stick", () => {
    window.history.replaceState(
      null,
      "",
      "/?collector=http%3A%2F%2Fparam.example%2F",
    );
    renderHook(() => useCollectorUrl());
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(
      "http://param.example/",
    );
  });

  it("setUrl updates the in-memory value, persists, and rewrites the param", () => {
    const { result } = renderHook(() => useCollectorUrl());

    act(() => result.current.setUrl("http://remote.example:9000"));

    expect(result.current.url).toBe("http://remote.example:9000");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(
      "http://remote.example:9000",
    );
    expect(window.location.search).toBe(
      "?collector=http%3A%2F%2Fremote.example%3A9000",
    );
  });

  it("setUrl trims whitespace and ignores empty input", () => {
    const { result } = renderHook(() => useCollectorUrl());

    act(() => result.current.setUrl("   "));
    expect(result.current.url).toBe(DEFAULT_COLLECTOR_URL);

    act(() => result.current.setUrl("  http://trimmed.example  "));
    expect(result.current.url).toBe("http://trimmed.example");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(
      "http://trimmed.example",
    );
  });

  it("setUrl back to the default removes the URL param", () => {
    const { result } = renderHook(() => useCollectorUrl());

    act(() => result.current.setUrl("http://remote.example/"));
    expect(window.location.search).toBe(
      "?collector=http%3A%2F%2Fremote.example%2F",
    );

    act(() => result.current.setUrl(DEFAULT_COLLECTOR_URL));
    expect(window.location.search).toBe("");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe(
      DEFAULT_COLLECTOR_URL,
    );
  });

  it("preserves other URL params and hash when rewriting", () => {
    window.history.replaceState(null, "", "/?other=1#section");
    const { result } = renderHook(() => useCollectorUrl());

    act(() => result.current.setUrl("http://remote.example/"));

    expect(window.location.search).toContain("other=1");
    expect(window.location.search).toContain(
      "collector=http%3A%2F%2Fremote.example%2F",
    );
    expect(window.location.hash).toBe("#section");
  });
});
