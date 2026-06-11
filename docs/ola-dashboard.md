# ola-dashboard

A browser dashboard for monitoring agent progress in real time — the
visually rich sibling of [ola-top](./ola-top.md), aimed at demos (a wave of
parallel agents sweeping a codebase) as well as monitoring. It is a **view over
the same files** ola-top reads: there is no collector and no background state.

```bash
ola-dashboard [-f <agent-folder>] [-p <port>] [--no-browser]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-f, --agent-folder` | Path to the agent folder | `../agent` |
| `-p, --port` | Port to listen on | `8765` |
| `--host` | Host/interface to bind | `127.0.0.1` |
| `--dist` | Path to the built SPA | `<repo>/dashboard/dist` |
| `--no-browser` | Don't open a browser on startup | off |

Build the SPA once before first use (it is served from `dashboard/dist`):

```bash
make dashboard          # npm install + vite build
ola-dashboard -f ../agent
```

## How it works

The `ola-dashboard` server is a thin, **stateless** HTTP server. It serves the
built single-page app plus a small JSON API, and re-parses the agent folder on
every request — nothing is cached between requests, so you can kill and restart
it at any time without losing state (the `.ola/` files are the source of truth).

| Route | Purpose |
|-------|---------|
| `GET /api/snapshot` | The current run state, built by `ola.monitor.data.build_snapshot` from `PLAN.md`, `.ola/tasks.json`, and `.ola/events.jsonl`. |
| `GET /api/concurrency?folder=…` | The live cap from `<folder>/.ola/concurrency` (`null` when no file yet). |
| `PUT /api/concurrency` | Write the cap — the dashboard's **only** write, driven by the parallel-agents slider. |

The SPA polls `/api/snapshot` (~1.5s) and replaces its whole view each tick.
Only folders running in parallel mode (those with a `.ola/` sidecar) appear —
they are the per-task spine the dashboard renders.

## Panels

* **Project picker** — the title is a dropdown over the agent subfolders found
  on disk; every panel is scoped to the picked one.
* **Hero metrics** — task counters (total / completed / failed / active), the
  run clock (frozen at the last terminal event), live fleet output tokens/sec,
  and the **parallel-agents slider** (writes `.ola/concurrency`).
* **Work-item heatmap** — one cell per task, every task visible from the start,
  coloured by status in dispatch order so the wave sweeps across the grid.
* **Activity feed** — recently completed tasks, newest first.
* **Metrics panel** — whatever each task published under its opaque `data`.

## ola-top vs ola-dashboard

Both read the same agent-folder files and share the task-id scheme and event
envelope (`src/ola/events/SCHEMA.md`). Reach for **ola-top** for a terminal,
zero-dependency look at token economics and throughput; reach for
**ola-dashboard** for a richer, projector-friendly view of a parallel run. See
the `ola-top` and `ola-dashboard` skills for the design philosophy behind each.

## Development

The SPA source lives in `dashboard/` (Vite + React + TypeScript). In dev mode
(`npm --prefix dashboard run dev`) Vite serves the SPA on :5173 and proxies
`/api/*` to the dashboard server on :8765, so run both:

```bash
ola-dashboard -f ../agent --no-browser   # API + (stale) built SPA on :8765
npm --prefix dashboard run dev           # hot-reloading SPA on :5173
```

`make dashboard-test` runs the SPA's lint and unit tests.
