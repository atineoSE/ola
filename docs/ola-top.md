# ola-top

A `top`-like terminal dashboard for monitoring agent progress in real time. Shows task completion, token usage, cache hit rates, and wall time for each phase — with per-iteration and per-task drill-down.

```bash
ola-top [-f <agent-folder>] [-r <refresh-seconds>]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-f, --agent-folder` | Path to the agent folder | `../agent` |
| `-r, --refresh` | Refresh interval in seconds | `2` |

## Keybindings

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate rows |
| `Enter` | Expand / collapse a folder to show its sub-rows |
| `m` | Toggle between the **task** view (default) and the **metrics** view |
| `PgUp` / `PgDn` | Scroll a viewport at a time |
| `q` | Quit |

## Views

ola-top has two column layouts, toggled with `m`:

* **Task view** (default) — folder/task progress: agent, model, tasks done, turns, wall time.
* **Metrics view** — token economics: input/output tokens, average and max context, cache hit rate, in/out ratio, LLM-vs-tool time, TTFT, tokens/sec.

## Sequential vs parallel folders

ola-top adapts what it shows when you expand a folder, based on whether the folder runs in [parallel mode](./parallel.md) (i.e. it has a `.ola/` sidecar):

* **Sequential folder** — expanding shows one **iteration** row per loop pass (`task-…`/`seed` phases from `STATS.jsonl`).
* **Parallel folder** — the header carries a `running N / cap M` badge (live workers vs the concurrency cap), and expanding shows one **task** row per task in `PLAN.md`, sourced from `.ola/tasks.json` and `.ola/events.jsonl`:
  * task text
  * status (`pending` / `running` / `complete` / `failed`, colour-coded)
  * the latest `working` progress message
  * attempt count
  * elapsed wall time (first→last event for that task)

> Per-task progress messages and elapsed time come from the `events.jsonl` stream. They populate as the run emits events; a folder that has only just started shows status from `tasks.json` with empty progress until the first events land.

## Example output

```
 ola-top — /Users/you/experiment/agent             03:42:15 PM

 # │ Folder              │ Tasks │   Input │  Output │ Cache% │  Time
 1 │ 01-setup            │   5/5 │  120.4k │   45.2k │  82.3% │  3m12s
 2 │ 02-implement  running 2 / cap 3
   │   └ Add retry to client │ running │ editing client.py │     │ 1 │  0m42s
   │   └ Wire timeout config │ complete│                   │     │ 1 │  1m05s

 q: quit  ↑↓: navigate  Enter: expand/collapse  m: toggle view
```
