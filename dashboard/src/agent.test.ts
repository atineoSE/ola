import { describe, expect, it } from "vitest";

import { agentColor, agentName, DEFAULT_ACCENT, inkOn } from "./agent";

describe("agentName", () => {
  it("maps the known backend mnemonics to display names", () => {
    expect(agentName("cc")).toBe("Claude Code");
    expect(agentName("ct")).toBe("Claude Code (TUI)");
    expect(agentName("oh")).toBe("OpenHands");
    expect(agentName("cx")).toBe("Codex");
  });

  it("falls back to the raw mnemonic for an unknown backend", () => {
    expect(agentName("xyz")).toBe("xyz");
  });

  it("renders an em-dash placeholder for an empty/absent backend", () => {
    expect(agentName("")).toBe("—");
    expect(agentName(null)).toBe("—");
    expect(agentName(undefined)).toBe("—");
  });
});

describe("agentColor", () => {
  it("maps the known backends to their signature theme colors", () => {
    expect(agentColor("cc")).toBe("#CB7153");
    expect(agentColor("oh")).toBe("#FFFF9B");
    expect(agentColor("cx")).toBe("#372FF5");
  });

  it("falls back to the default accent for unknown/empty backends", () => {
    expect(agentColor("xyz")).toBe(DEFAULT_ACCENT);
    expect(agentColor("")).toBe(DEFAULT_ACCENT);
    expect(agentColor(null)).toBe(DEFAULT_ACCENT);
    expect(agentColor(undefined)).toBe(DEFAULT_ACCENT);
  });
});

describe("inkOn", () => {
  it("picks dark ink on a light fill and light ink on a dark fill", () => {
    expect(inkOn("#FFFF9B")).toBe("#0b0d10"); // OpenHands pale yellow → dark ink
    expect(inkOn("#CB7153")).toBe("#f5f7fa"); // Claude terracotta → light ink
    expect(inkOn("#372FF5")).toBe("#f5f7fa"); // Codex indigo → light ink
  });

  it("handles white and black extremes and a missing leading hash", () => {
    expect(inkOn("#ffffff")).toBe("#0b0d10");
    expect(inkOn("#000000")).toBe("#f5f7fa");
    expect(inkOn("ffffff")).toBe("#0b0d10");
  });
});
