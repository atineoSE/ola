import { describe, expect, it } from "vitest";

import {
  describeMetrics,
  formatElapsed,
  formatTokensPerSec,
  metricEntries,
  readMetrics,
} from "./format";

/** Build a `data` payload carrying a metrics block. */
function withMetrics(
  output_tokens: number,
  decode_ms: number,
  tokens_per_sec: number,
): Record<string, unknown> {
  return { metrics: { output_tokens, decode_ms, tokens_per_sec } };
}

describe("formatElapsed", () => {
  it("renders --:-- for null/undefined (not-started sentinel)", () => {
    expect(formatElapsed(null)).toBe("--:--");
    expect(formatElapsed(undefined)).toBe("--:--");
  });

  it("renders --:-- for non-finite or negative inputs", () => {
    expect(formatElapsed(NaN)).toBe("--:--");
    expect(formatElapsed(-3)).toBe("--:--");
  });

  it("pads minutes and seconds to two digits", () => {
    expect(formatElapsed(0)).toBe("00:00");
    expect(formatElapsed(7)).toBe("00:07");
    expect(formatElapsed(65)).toBe("01:05");
  });

  it("floors fractional seconds rather than rounding", () => {
    expect(formatElapsed(7.9)).toBe("00:07");
  });

  it("overflows the minutes field past 60 instead of rolling into hours", () => {
    expect(formatElapsed(60 * 75 + 12)).toBe("75:12");
  });
});

describe("formatTokensPerSec", () => {
  it("renders — for null/undefined/non-finite", () => {
    expect(formatTokensPerSec(null)).toBe("—");
    expect(formatTokensPerSec(undefined)).toBe("—");
    expect(formatTokensPerSec(NaN)).toBe("—");
  });

  it("renders to one decimal", () => {
    expect(formatTokensPerSec(0)).toBe("0.0");
    expect(formatTokensPerSec(46.04)).toBe("46.0");
  });
});

describe("readMetrics", () => {
  it("returns null when there is no metrics block", () => {
    expect(readMetrics(null)).toBeNull();
    expect(readMetrics(undefined)).toBeNull();
    expect(readMetrics({})).toBeNull();
    expect(readMetrics({ message: "working" })).toBeNull();
  });

  it("returns null when tokens_per_sec is not a finite number", () => {
    expect(readMetrics({ metrics: { output_tokens: 5 } })).toBeNull();
    expect(
      readMetrics({ metrics: { tokens_per_sec: "fast" } }),
    ).toBeNull();
  });

  it("extracts the typed block, defaulting missing counters to 0", () => {
    expect(readMetrics(withMetrics(487, 10579, 46.0))).toEqual({
      output_tokens: 487,
      decode_ms: 10579,
      tokens_per_sec: 46.0,
    });
    expect(readMetrics({ metrics: { tokens_per_sec: 30 } })).toEqual({
      output_tokens: 0,
      decode_ms: 0,
      tokens_per_sec: 30,
    });
  });
});

describe("metricEntries", () => {
  it("returns [] when there is no metrics block", () => {
    expect(metricEntries(null)).toEqual([]);
    expect(metricEntries({})).toEqual([]);
    expect(metricEntries({ message: "running" })).toEqual([]);
  });

  it("renders throughput then volume from the metrics block", () => {
    expect(metricEntries(withMetrics(487, 10579, 46.04))).toEqual([
      { key: "tok/s", value: "46.0" },
      { key: "tokens", value: "487" },
    ]);
  });
});

describe("describeMetrics", () => {
  it("joins the metrics with a middle dot", () => {
    expect(describeMetrics(withMetrics(487, 10579, 46.0))).toBe(
      "tok/s: 46.0 · tokens: 487",
    );
  });

  it("returns an empty string when there are no metrics", () => {
    expect(describeMetrics({ message: "running" })).toBe("");
    expect(describeMetrics(null)).toBe("");
  });
});
