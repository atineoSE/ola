# ola-top

A `top`-like terminal dashboard for monitoring agent progress in real time. Shows task completion, token usage, cache hit rates, and wall time for each phase — with per-iteration and per-task drill-down.

```bash
ola-top [-f <agent-folder>] [-r <refresh-seconds>]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-f, --agent-folder` | Path to the agent folder | `../agent` |
| `-r, --refresh` | Refresh interval in seconds | `2` |

Only subfolders that contain a `PLAN.md` are listed — that file is the task
spine ola-top reads, so a directory without one (e.g. a `bin/` of helper
scripts living beside the numbered folders) is not a run and never appears.

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

In a parallel folder the **Agent** fills from the `started` event the moment a task begins (mnemonic only — the agent version arrives with the first `STATS.jsonl` row). **Model** is not in the event stream, so it stays blank until that first `STATS.jsonl` row lands.

## Sequential vs parallel folders

ola-top adapts what it shows when you expand a folder, based on whether the folder runs in [parallel mode](./parallel.md) (i.e. it has a `.ola/` sidecar):

* **Sequential folder** — expanding shows one **iteration** row per loop pass (`task-…` phases from `STATS.jsonl`).
* **Parallel folder** — expanding shows one **task** row per task in `PLAN.md`, sourced from `.ola/tasks.json` and `.ola/events.jsonl`. Each sub-row sticks to the same columns as everything else — it only fills the ones a single task has a value for, and leaves the rest blank:
  * **Folder** — the task id followed by the task text. The task id (e.g. `t-1a2b3c4d`) is the same key used in `PLAN.md`, `.ola/tasks.json`, and `.ola/events.jsonl`, so you can grep it straight back into those files.
  * **Time** — worked time for the task (summed across its events, excluding idle gaps longer than two minutes), blank until the first events land.
  * **status** is conveyed by the row colour (`pending` / `running` / `complete` / `failed` / `blocked`), not a text column.
  * Agent, Model, Tasks, and Turns stay blank — a single task carries no per-task value for them.

> Elapsed time comes from the `events.jsonl` stream and populates as the run emits events; a folder that has only just started shows its tasks from `tasks.json` with an empty Time column until the first events land.

> For a parallel folder, the **Folder row's Time** is the worked span across *all* its events — the sum of gaps between consecutive events, **excluding any gap longer than two minutes** — recomputed on every refresh. Dropping long gaps means an idle stretch, or the gap between an aborted run and a re-run that share one `events.jsonl`, doesn't inflate the number into a wall-clock span (so a dangling/aborted run no longer shows a runaway clock). It is derived from `events.jsonl` rather than summed from `STATS.jsonl`, so an interrupted-then-resumed run reports its true elapsed time instead of a stale number that can read shorter than a single task. Sequential folders keep summing per-iteration wall time from `STATS.jsonl`.

## Example output

```
 ola-top — /Users/you/experiment/agent             03:42:15 PM

 # │ Folder                          │ Agent │ Model         │ Tasks │ Turns │  Time
 1 │ 01-setup                        │ cc    │ claude-opus-4 │   5/5 │    18 │  3m12s
 2 │ ▼ 02-implement                  │ cc    │ claude-opus-4 │   1/3 │    24 │  4m20s
   │   └ t-1a2b3c4d Add retry to client                                   │  0m42s
   │   └ t-9f8e7d6c Wire timeout config                                   │  1m05s

 q: quit  ↑↓: navigate  Enter: expand/collapse  m: toggle view
```

Folder rows also read off colour: **green** = all tasks done, **bold yellow** = the active folder (the first one with work remaining), **plain yellow** = other in-progress folders, **dim** = no tasks yet. The active folder is shown by its colour alone — there is no separate marker.

Task sub-rows colour the whole row by status (here `running` and `complete`), so the status reads off the colour rather than a column.
