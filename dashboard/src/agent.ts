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
