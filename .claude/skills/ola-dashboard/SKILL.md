---
name: ola-dashboard
description: Design philosophy and scope guardrails for ola-dashboard, the browser monitor. Load whenever changing ola-dashboard — every change must be checked against this philosophy.
---

# The ola-dashboard design philosophy

ola-dashboard is the **visually rich, browser-based view of an ola run**. It is
the richer sibling of [[ola-top]]: same runs, same files, same task-id scheme —
a second, more demo-friendly window onto the work, not a different system. It
leans toward **demos** (a wave of agents sweeping a codebase, looking good on a
screen) but is also a real **monitoring** surface.

It was folded in from a prototype ("parallel-refactor") that was built around a
yt-dlp demo. That demo framing is just an example — **drop it**. The dashboard
is project-agnostic: `folder` is an opaque grouping key, work is free-text
`task_text` keyed by a stable `task_id`.

## Source of truth: the agent-folder files, no collector

This is the load-bearing rule. The dashboard is driven by **the same files
[[ola-top]] reads** — it is a *view* over file state, never its own state store:

- `<folder>/PLAN.md` — the task list (checkbox-is-truth).
- `<folder>/STATS.jsonl` — per-iteration stats.
- `<folder>/.ola/tasks.json` — per-task spine (seeds every task as `pending`
  before any agent runs, so the grid is fully populated from the start).
- `<folder>/.ola/events.jsonl` — lifecycle stream; envelope pinned to
  `src/ola/events/SCHEMA.md` (authoritative).
- `<folder>/.ola/concurrency` — the live parallel-agents cap.

The prototype routed all of this through a **collector** (a FastAPI service
aggregating `POST /events` + `/manifest` in memory and re-streaming over SSE).
**That layer is being removed.** Everything it served — task spine, per-task
status/`data`, run clock (`first_started_ts` / `last_terminal_ts`), counters,
fleet tokens/sec, concurrency — is already in the `.ola/` files. A collector is
a background process that starts, stops, holds memory, and loses all state when
killed; the files already are the durable, auditable source of truth. Reuse
them. Do not reintroduce a daemon, socket, or in-memory aggregator.

The one **write** the dashboard performs is the concurrency slider, which sets
`<folder>/.ola/concurrency` — exactly the live-edited file model ola already
uses. No other dashboard control mutates run state.

## Feature set (carried over, demo framing stripped)

The shape worth keeping from the prototype:

- **Project picker** — the title is a dropdown over the folders found on disk;
  every panel is scoped to the picked one.
- **Hero metrics** — counters (total / completed / failed / active), the run
  clock (elapsed, frozen at the last terminal event), live fleet output
  tokens/sec, and the **parallel-agents slider** (writes `.ola/concurrency`).
- **Work-item heatmap** — space-filling grid, every task visible from the
  start, coloured by status in dispatch order; the signature demo visual.
- **Activity feed** — scrolling list of completed tasks.
- **Metrics panel** — aggregate throughput/economics.
- Everything fits the viewport.

## Checklist when changing ola-dashboard

Challenge every change against these; push back if the answer is wrong:

- Does it read run state from the `.ola/*` files, or does it (re)introduce a
  collector / daemon / socket / server-side aggregator? Removing that layer is
  the whole point — don't add it back.
- Is there exactly one source of truth (the files), shared with [[ola-top]], or
  does this change create a second, divergent store?
- Is the only state-mutating control still the concurrency slider writing
  `.ola/concurrency`? A new control that writes anything else needs a hard
  justification.
- Does it stay project-agnostic (opaque `folder`, free-text tasks), or does it
  smuggle back yt-dlp / file-tree / per-language demo assumptions?
- Does it honor the event envelope and task-id scheme in
  `src/ola/events/SCHEMA.md` so a task greps back to `PLAN.md` / `tasks.json` /
  `events.jsonl` identically to ola-top?
- Is the new feature a *view* of existing file data, or does it demand the
  harness emit something new? If new, the field goes into the schema the
  harness writes — not a dashboard-only side channel.
- Is "visually rich for demos" earning its keep, or is it bloat that turns the
  dashboard into its own application? When in doubt, the simpler terminal
  answer already exists in [[ola-top]].
