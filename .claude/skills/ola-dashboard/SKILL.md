---
name: ola-dashboard
description: Design philosophy and scope guardrails for ola-dashboard, the browser monitor. Load whenever changing ola-dashboard — every change must be checked against this philosophy.
version: 1.4.0
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
- `<folder>/.ola/metrics.jsonl` — **optional** progress-probe samples. Written by
  **the harness, not the dashboard** (see below).

### The optional progress probe

The dashboard can surface a project-defined **progress metric** (e.g. tests
passing, files migrated, a coverage percent) in the hero metrics. The mechanism
keeps the no-collector / one-write invariants intact:

- The **harness** runs a user-configured probe command on an interval and
  appends each `{"name": …, "value": <number>}` sample to
  `<folder>/.ola/metrics.jsonl`. This is another harness-owned `.ola/` file,
  exactly like `events.jsonl` or `tasks.json` — the dashboard never runs the
  probe, never spawns a process, and never writes this file.
- The **dashboard** reads `metrics.jsonl` through `build_snapshot` (folding the
  samples into each folder's `progress` field — `{value, series}`) and renders a
  tile + sparkline. It stays a pure-read **view** of file state.
- When no probe is configured the file simply does not exist; `progress` is
  empty and the dashboard renders nothing extra, leaving the layout unchanged.

So the feature adds a metric *display* without adding a collector, daemon, or a
second state store, and without giving the dashboard a second write: the harness
owns the only producer of `metrics.jsonl`, the dashboard owns only its read.

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
  every panel is scoped to the picked one. The list spans finished, running,
  and **future** runs: a folder with only a `PLAN.md` (no `.ola/tasks.json`
  spine yet) is listed too, its checkboxes seeded as `pending` so an upcoming
  run can be previewed before the harness starts it — `build_snapshot` re-scans
  disk every poll, so the list tracks folders/plans as the agent writes them.
  Before the user picks, the default follows the work: the run with an agent
  active right now, else the last folder in run order with outstanding work
  (the frontier), else the last folder once everything is done.
- **Agent identity + theme** — alongside the title, the picked run names its
  agent (`agent_backend` → "Claude Code" / "OpenHands" / "Codex") and the
  model(s) it is driving, and the whole dashboard's accent recolors to the
  agent's signature color (cc `#CB7153`, oh `#FFFF9B`, cx `#372FF5`) so a glance
  tells you which agent is running. The model name is **not** in the event
  envelope — it is surfaced from `STATS.jsonl` through the snapshot, a view of
  existing file data, not a new harness emission.
- **Hero metrics** — counters (total / completed / failed / active), the
  elapsed clock (an **active-time stopwatch**: it advances only while ≥1 agent
  is running and freezes during idle gaps, so it reads as time-worked, not
  wall-clock — `build_snapshot` accumulates the active seconds from the event
  stream and hands back an anchor ts to tick the open tail), live fleet output
  tokens/sec (the *current* windowed rate, which blanks to `—` when no agent is
  decoding; the **avg and peak beneath it are file-derived** — avg = total
  output tokens over active runtime, peak = the fastest task's lifetime rate —
  so they persist after the run ends and survive a reload, unlike a client-side
  session accumulator), total output tokens in millions (a running total to
  watch climb), and the **parallel-agents slider** (writes `.ola/concurrency`).
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
