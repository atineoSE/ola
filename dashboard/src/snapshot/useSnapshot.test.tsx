import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSnapshot } from "./useSnapshot";
import type { Snapshot } from "./types";

const SNAP: Snapshot = {
  first_started_ts: "2026-05-27T14:00:00.000Z",
  counters: { total_tasks: 1, completed: 0, failed: 0, active: 1 },
  tasks: {
    "t-1": {
      task_id: "t-1",
      task_text: "Task",
      folder: "09-par",
      agent_backend: "cc",
      status: "working",
      attempt: 0,
      data: {},
    },
  },
  folders: {
    "09-par": {
      first_started_ts: "2026-05-27T14:00:00.000Z",
      last_terminal_ts: null,
      project: "09-par",
    },
  },
  activity: [],
};

function jsonResponse(body: unknown, ok = true, statusCode = 200): Response {
  return {
    ok,
    status: statusCode,
    json: async () => body,
  } as Response;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useSnapshot", () => {
  it("starts connecting with an empty snapshot, then opens after the first poll", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(SNAP));
    const { result } = renderHook(() =>
      useSnapshot({ fetchImpl: fetchImpl as unknown as typeof fetch }),
    );

    // First render: nothing fetched yet.
    expect(result.current.snapshot.tasks).toEqual({});
    expect(result.current.status).toBe("connecting");

    await waitFor(() => expect(result.current.status).toBe("open"));
    expect(result.current.snapshot).toEqual(SNAP);
    expect(fetchImpl).toHaveBeenCalledWith("/api/snapshot");
  });

  it("flags closed and keeps the last snapshot when a poll fails", async () => {
    let call = 0;
    const fetchImpl = vi.fn(async () => {
      call += 1;
      if (call === 1) return jsonResponse(SNAP);
      throw new Error("server down");
    });
    const { result } = renderHook(() =>
      useSnapshot({
        refreshMs: 5,
        fetchImpl: fetchImpl as unknown as typeof fetch,
      }),
    );

    // `closed` is only reachable after the first poll succeeded (it starts
    // `connecting`), so once we see it the last good snapshot must be retained.
    await waitFor(() => expect(result.current.status).toBe("closed"));
    expect(result.current.snapshot).toEqual(SNAP);
  });

  it("treats a non-ok response as a failed poll", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}, false, 500));
    const { result } = renderHook(() =>
      useSnapshot({ fetchImpl: fetchImpl as unknown as typeof fetch }),
    );
    await waitFor(() => expect(result.current.status).toBe("closed"));
    expect(result.current.snapshot.tasks).toEqual({});
  });
});
