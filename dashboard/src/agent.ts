/*
 * Agent identity and theming, keyed off the `agent_backend` mnemonic the event
 * envelope carries (`src/ola/events/SCHEMA.md`: `"cc"` / `"oh"` / `"cx"`).
 *
 * The dashboard recolors itself per backend so a glance tells you which agent
 * is driving the run — Claude Code's terracotta, OpenHands' pale yellow, Codex's
 * indigo. Kept React-free and pure so it can be unit-tested and reused by any
 * widget (header, hero accent, badges).
 */

/** Full, human display name for an agent backend mnemonic. */
const AGENT_NAMES: Record<string, string> = {
  cc: "Claude Code",
  oh: "OpenHands",
  cx: "Codex",
};

/** Per-backend accent color (the run's theme). Falls back to the default sky
 * accent for an unknown/empty backend so the dashboard never loses its accent. */
const AGENT_COLORS: Record<string, string> = {
  cc: "#CB7153",
  oh: "#FFFF9B",
  cx: "#372FF5",
};

/** Default accent when no backend is known yet (matches `--color-accent`). */
export const DEFAULT_ACCENT = "#7dd3fc";

/** Ink colors for text laid directly over an agent color: the theme's near-black
 * (`--color-bg`) on light backgrounds, its off-white (`--color-text`) on dark. */
const INK_DARK = "#0b0d10";
const INK_LIGHT = "#f5f7fa";

/** sRGB relative luminance (WCAG) of a `#rrggbb` color, 0 (black) → 1 (white). */
function luminance(hex: string): number {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return 0;
  const n = parseInt(m[1], 16);
  const channel = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const r = channel((n >> 16) & 0xff);
  const g = channel((n >> 8) & 0xff);
  const b = channel(n & 0xff);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/**
 * Readable ink for text placed on `color`: dark on a light fill, light on a
 * dark one (threshold at mid-luminance). Lets the page background be an agent
 * color while keeping the header legible across all three (oh's pale yellow
 * takes dark ink; cc's terracotta and cx's indigo take light ink).
 */
export function inkOn(color: string): string {
  return luminance(color) > 0.5 ? INK_DARK : INK_LIGHT;
}

/** Display name for a backend mnemonic, or the raw mnemonic when unmapped. */
export function agentName(backend: string | null | undefined): string {
  if (!backend) return "—";
  return AGENT_NAMES[backend] ?? backend;
}

/** Theme accent color for a backend, or the default sky accent when unknown. */
export function agentColor(backend: string | null | undefined): string {
  if (!backend) return DEFAULT_ACCENT;
  return AGENT_COLORS[backend] ?? DEFAULT_ACCENT;
}
