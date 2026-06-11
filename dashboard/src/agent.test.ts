import { describe, expect, it } from "vitest";

import { agentColor, agentName, DEFAULT_ACCENT } from "./agent";

describe("agentName", () => {
  it("maps the known backend mnemonics to display names", () => {
    expect(agentName("cc")).toBe("Claude Code");
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
