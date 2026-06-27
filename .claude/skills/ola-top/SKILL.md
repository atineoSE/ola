---
name: ola-top
description: Design philosophy and scope guardrails for ola-top, the terminal monitor. Load whenever changing ola-top — every change must be checked against this philosophy.
version: 1.1.1
---

# The ola-top design philosophy

ola-top is a **`top`-like terminal tool** for watching an ola run. It is
simple, it holds no state of its own, and it reads **only from the files the
harness already writes** under the agent folder. It is the always-available,
zero-dependency monitor; [[ola-dashboard]] is the richer visual sibling, not a
replacement.

ola-top exists to answer "is the infrastructure healthy and moving" — token
economics and throughput — at a glance from a terminal. It is for
**monitoring**, not demos.

## Source of truth: files, never a service

ola-top parses the agent folder directly and derives everything on each
refresh tick. It never runs a collector, daemon, socket, or background
aggregator, and it never caches run state across ticks. The files it reads:

- `<folder>/PLAN.md` — task counts (checkbox-is-truth, same as the harness).
  This file is also the gate for listing a folder at all: a subdirectory with no
  `PLAN.md` is not a run and is skipped, so helper dirs (e.g. an `agent/bin/`)
  beside the numbered folders never show up as rows.
- `<folder>/STATS.jsonl` — per-iteration stats (the metrics spine).
- `<folder>/.ola/tasks.json` — per-task spine for parallel folders.
- `<folder>/.ola/events.jsonl` — lifecycle stream; envelope pinned to
  `src/ola/events/SCHEMA.md` (authoritative — read it before touching event
  parsing).
- `<folder>/.ola/concurrency` — live cap (read-only here; the harness owns it).

The data layer is `src/ola/monitor/data.py`; the UI is
`src/ola/monitor/ui.py`. The user-facing reference (flags, keybindings,
columns, sequential-vs-parallel expansion) is `docs/ola-top.md` — keep it in
lock-step with behavior changes.

## Two views, one column grammar

ola-top has exactly two column layouts, toggled with `m`:

- **Task view** (default) — folder/task progress.
- **Metrics view** — token economics and throughput.

Expanding a folder drills into iterations (sequential) or per-task sub-rows
(parallel). Sub-rows share the same columns as everything else and only fill
the ones a single task has a value for — **honor the columns**; status reads
off row colour, not an extra text column. Resist adding a third view or
bolting panels onto the grid; that pressure belongs in [[ola-dashboard]].

A bold **`TOTAL`** footer row is pinned to the bottom (below a divider, after the
scrolled window) as a grand total across all folders. It is *within* the column
grammar, not a bolted-on panel: it fills only the **additive** numeric columns
(Tasks, Turns, Time; and Input, Output, Avg/Max Ctx in metrics view) and leaves
ratio/percentage/median columns (Cache%, In/Out, LLM/Tool, TTFT, Tok/s) blank —
a sum of a median or a rate is meaningless, so honour the columns by not
fabricating one. It is excluded from the cursor/`display_rows` navigation (never
selectable) and from the `N/total` indicator; reserve its two lines (row +
divider) in `_TABLE_CHROME_ROWS` so the viewport math still fits.

## One display row is one terminal line

The viewport scroll budgets exactly one terminal line per display row
(`max_rows = lines - _TABLE_CHROME_ROWS`). That invariant is load-bearing: if a
cell wraps, a row spans two-plus lines and the table overflows the screen — the
single-page layout silently breaks (a real bug a 25-folder run hit when the
verbose Agent string folded to three lines per row). So **every column is
`no_wrap=True` and truncates with `overflow="ellipsis"`**, never folds; the
`Folder` column alone is flexible (`ratio=1`) so it absorbs slack and ellipsizes
first, leaving the fixed numeric columns their full width. The title and caption
are likewise pinned to one line each (their `no_wrap`/`overflow`), matching the
single line `_TABLE_CHROME_ROWS` reserves for each. When you add or widen a
column, keep it single-line and re-check that a row never wraps at narrow widths.

## Checklist when changing ola-top

Challenge every change against these; push back if the answer is wrong:

- Does it read from files the harness already writes, or does it introduce a
  collector/daemon/socket/in-memory run state? (The latter is the dashboard's
  old mistake — don't import it here.)
- Does it stay a terminal tool — no web server, no browser, no rich-media
  rendering?
- Does it keep the two-view model (task / metrics) and the shared column
  grammar, or does it grow a third view / freeform panel?
- Is the new datum already in `PLAN.md` / `STATS.jsonl` / `.ola/*`? If it
  needs a new field, does that field get added to the schema the harness
  writes — not invented in the monitor?
- Does it stay backend-agnostic (cc / oh / cx), reading the envelope's
  `agent_backend` rather than branching on a specific agent?
- Does it tolerate partially-written / malformed files (skip the bad line),
  so a live run never crashes the monitor?
- Is this monitoring, or is it demo polish that belongs in [[ola-dashboard]]?
