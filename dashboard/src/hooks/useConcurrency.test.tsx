import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useConcurrency } from "./useConcurrency";

type FetchCall = { url: string; init?: RequestInit };

let calls: FetchCall[];
let getResponse: () => Response;
let fetchImpl: typeof fetch;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  calls = [];
  getResponse = () => jsonResponse({ folder: "01-unit-tests", concurrency: 8 });
  fetchImpl = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (init?.method === "PUT") {
      return Promise.resolve(jsonResponse({ status: "accepted" }, 202));
    }
    return Promise.resolve(getResponse());
  }) as unknown as typeof fetch;
});

afterEach(() => {
  vi.useRealTimers();
});

async function renderAndSettle(folder: string | null = "01-unit-tests") {
  const rendered = renderHook(() => useConcurrency(folder, { fetchImpl }));
  await act(async () => {});
  return rendered;
}

describe("useConcurrency", () => {
  it("loads the current target for the folder (same-origin)", async () => {
    const { result } = await renderAndSettle();
    expect(calls[0].url).toBe("/api/concurrency?folder=01-unit-tests");
    expect(result.current.available).toBe(true);
    expect(result.current.target).toBe(8);
  });

  it("reports null target when no concurrency file exists yet", async () => {
    getResponse = () =>
      jsonResponse({ folder: "01-unit-tests", concurrency: null });
    const { result } = await renderAndSettle();
    expect(result.current.available).toBe(true);
    expect(result.current.target).toBeNull();
  });

  it("is unavailable when the GET fails", async () => {
    getResponse = () => jsonResponse({ detail: "boom" }, 500);
    const { result } = await renderAndSettle();
    expect(result.current.available).toBe(false);
  });

  it("is unavailable without a folder", async () => {
    const { result } = await renderAndSettle(null);
    expect(result.current.available).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it("setTarget updates optimistically and PUTs debounced", async () => {
    const { result } = await renderAndSettle();
    vi.useFakeTimers();

    act(() => result.current.setTarget(12));
    act(() => result.current.setTarget(13));
    expect(result.current.target).toBe(13); // optimistic
    expect(calls.filter((c) => c.init?.method === "PUT")).toHaveLength(0);

    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    const puts = calls.filter((c) => c.init?.method === "PUT");
    expect(puts).toHaveLength(1); // debounced: only the last step
    expect(puts[0].url).toBe("/api/concurrency");
    expect(JSON.parse(puts[0].init?.body as string)).toEqual({
      folder: "01-unit-tests",
      concurrency: 13,
    });
  });
});
