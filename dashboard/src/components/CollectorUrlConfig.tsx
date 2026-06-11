/*
 * Inline collector-URL editor for the dashboard header.
 *
 * In display mode it shows the current collector origin next to an "Edit"
 * button. In edit mode it swaps in a text input plus Save/Cancel buttons
 * and the form submits on Enter — the same affordance you get from the
 * browser URL bar, so a demo operator can re-point the dashboard at a
 * remote collector mid-session without touching the URL.
 */

import { useEffect, useRef, useState } from "react";

export interface CollectorUrlConfigProps {
  url: string;
  onChange: (next: string) => void;
}

export function CollectorUrlConfig({ url, onChange }: CollectorUrlConfigProps) {
  // `null` means "display mode"; a string means "editing, here's the draft".
  // Storing it as a single piece of state keeps the (editing, draft) pair
  // atomic — clicking Edit seeds the draft from `url` in the same update.
  const [draft, setDraft] = useState<string | null>(null);
  const editing = draft !== null;
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (editing) {
      // Focus and select-all so the operator can replace the URL in one
      // keystroke. setTimeout(0) defers until after the input has mounted.
      const id = window.setTimeout(() => inputRef.current?.select(), 0);
      return () => window.clearTimeout(id);
    }
  }, [editing]);

  if (draft === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-text-muted">
        <span className="uppercase tracking-wider">Collector</span>
        <code
          data-testid="collector-url-display"
          className="rounded bg-bg-panel px-2 py-1 font-mono text-xs text-text"
        >
          {url}
        </code>
        <button
          type="button"
          onClick={() => setDraft(url)}
          className="rounded border border-border px-2 py-1 text-xs hover:bg-bg-panel"
        >
          Edit
        </button>
      </div>
    );
  }

  const submit = () => {
    const trimmed = draft.trim();
    if (trimmed.length === 0) return;
    onChange(trimmed);
    setDraft(null);
  };

  return (
    <form
      onSubmit={(ev) => {
        ev.preventDefault();
        submit();
      }}
      className="flex items-center gap-2 text-sm"
    >
      <label
        htmlFor="collector-url-input"
        className="uppercase tracking-wider text-text-muted"
      >
        Collector
      </label>
      <input
        id="collector-url-input"
        ref={inputRef}
        type="url"
        value={draft}
        onChange={(ev) => setDraft(ev.target.value)}
        onKeyDown={(ev) => {
          if (ev.key === "Escape") {
            ev.preventDefault();
            setDraft(null);
          }
        }}
        placeholder="http://localhost:8000"
        className="w-72 rounded border border-border bg-bg-panel px-2 py-1 font-mono text-xs text-text outline-none focus:border-accent"
      />
      <button
        type="submit"
        className="rounded border border-border bg-bg-panel px-2 py-1 text-xs hover:border-accent"
      >
        Save
      </button>
      <button
        type="button"
        onClick={() => setDraft(null)}
        className="rounded px-2 py-1 text-xs text-text-muted hover:text-text"
      >
        Cancel
      </button>
    </form>
  );
}
