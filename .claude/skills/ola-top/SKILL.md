---
name: ola-top
description: Design philosophy and scope guardrails for ola-top, the terminal monitor. Load whenever changing ola-top — every change must be checked against this philosophy.
version: 1.0.1
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
