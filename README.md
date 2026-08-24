# OLA: Outer Loop of Agents — A harness to run long-horizon agentic loops

`ola` is a light harness that allows to run AI coding agents for long-horizon tasks. With a simple folder structure and a few markdown files, you can direct long-running coding tasks. 

The implementation follows the [Ralph Wiggum technique](https://ghuntley.com/ralph/): a way for the agent to iterate on fresh contexts as it works relentlessly against tasks in a plan file. The design is heavily influenced by [this presentation](https://youtu.be/5syeNjq2ZCU?si=a2RvALDjiXPfYqJn) from [Ray Myers](https://github.com/raymyers), Chief Architect at [OpenHands](https://openhands.dev).

There are 3 agents currently supported:
* [Claude Code](https://github.com/anthropics/claude-code) — headless (`cc`) and an
  experimental interactive-TUI variant (`ct`, see below)
* [OpenHands CLI](https://github.com/OpenHands/software-agent-sdk) (`oh`)
* [Codex](https://github.com/openai/codex)

## Install

```bash
git clone git@github.com:atineoSE/ola.git && cd ola
git checkout vX.Y.Z        # or stay on main for the development version
uv tool install .
```

The checkout determines everything: `ola --version` reports the version in
`pyproject.toml`, and `ola-sandbox` pulls the matching sandbox template image
(`ghcr.io/atineose/ola:<that version>`) — you never type an image tag. See
[docs/sandbox.md](./docs/sandbox.md) for the full resolution order, and
`.claude/skills/ola-release/SKILL.md` for how releases are cut.

## Usage

```bash
ola [-f <agent-folder>] [-a cc|ct|oh|codex] [-m MODEL] [-l LIMIT] [--max-attempts N] [-v]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-f, --agent-folder` | Path to the agent folder | `../agent` |
| `-a, --agent` | Agent: `cc`/`claude-code`, `ct`/`claude-tui`, `oh`/`openhands`, or `cx`/`codex` | `cc` |
| `-m, --model` | Model name | Agent default |
| `-l, --limit` | Max iterations per subfolder (ignored in parallel mode) | No limit |
| `--max-attempts` | Total-attempts ceiling for failed/stagnant tasks | `3` |
| `-v, --verbose` | Debug logging | Off |

## Folder structure

```
project/                # workspace root (the example ships this as dummy-project/)
  project/              # your source code, the project repo (must be a git repo)
  agent/                # ola agent folder (git repo created by ola if missing)
    .env                # LLM_BASE_URL, LMNR_BASE_URL, etc. (gitignored)
    allowlist.txt       # Optional: Domain list to allow inside the sandbox
    provision.sh        # Optional: extra tooling, installed into the sandbox
    run-init.sh         # Optional: preconditions, run once before each ola run
    01-setup/           # Plan subfolder
      PLAN.md           # Required: markdown todo list of independent tasks
      TASK-PROMPT.md    # Optional: per-task prompt template
      .claude/          # Claude Code config dir (auto-created by ola)
        projects/...    # conversation history auto-created by claude
      .openhands/       # OpenHands state dir (auto-created by ola)
        logs/
        trajectories/
      .codex/           # Codex state dir (auto-created by ola)
        config.toml     # generated per-phase by ola
        last.txt        # last assistant message written by codex
      .ola/             # Parallel-mode sidecar (auto-created; see below)
        concurrency     # Optional: integer concurrency cap for this folder
        tasks.json      # Per-task state spine
        events.jsonl    # Event stream (audit trail; read by ola-top)
        worktrees/      # One git worktree per in-flight task
    02-implement/
      TASK-PROMPT.md
      PLAN.md
      .claude/
      .openhands/
      .codex/
      .ola/
```

### Ordering: folders are sequential, tasks are parallel

The plan structure carries an implicit contract with two levels:

* **Tasks within one `PLAN.md` are independent and parallel-safe.** ola may run them concurrently (see [Parallel execution](#parallel-execution)) and gives no guarantee about their relative order. Never write a task that relies on a sibling task in the same plan having run first — even at a concurrency cap of 1, order is not part of the contract.
* **Ordering comes from folders.** Plan subfolders are processed strictly in name order (`01-…`, `02-…`, …), and a folder must fully complete before the next one starts.

To express dependent work, split it into indexed `NN-description/` folders — one folder per dependency stage — each with its own `PLAN.md` of mutually independent tasks.

Each plan subfolder must contain a `PLAN.md` — the authored plan that drives the loop. (A folder with no `PLAN.md` is skipped: that is how the janitor parks human-only work behind a `BLOCKERS.md`.) You can write these folders by hand, but the intended workflow is to settle a plan with an agent in an ordinary planning session and then run the **`ola-plan` skill** (`.claude/skills/ola-plan/`), which decomposes that plan into the folder tree — sequential stages as numbered folders, parallel-safe work as tasks within one `PLAN.md`. See [Authoring a plan with the `ola-plan` skill](#authoring-a-plan-with-the-ola-plan-skill).

While `PLAN.md` has unchecked tasks (`- [ ]`), ola dispatches each one to the agent using `TASK-PROMPT.md` — a per-task template with `{{task_text}}` and `{{task_id}}` placeholders. A task is done once its checkbox is ticked; the folder completes when every box is checked. See [Parallel execution](#parallel-execution) for how tasks are scheduled and isolated.

`TASK-PROMPT.md` is optional and falls back to a sensible default if missing. However, it is recommended to write one, since it's key to driving the agent reliably through each task.

The agent folder must be its own git repository (ola initialises one if missing). ola commits to this repo after each completed task, tracking plan progress independently from your source code.

Each agent gets a per-phase state directory (`.claude/` or `.openhands/`) inside each plan subfolder. For Claude Code, `CLAUDE_CONFIG_DIR` is set to `.claude/`, giving each phase its own conversation history that persists across sandbox sessions. For OpenHands, logs and trajectories are written to `.openhands/logs/` and `.openhands/trajectories/`.

## Authoring a plan with the `ola-plan` skill

ola does not generate its own plan. You bring a plan you have already settled
with an agent in an ordinary planning session — outside the ola harness, going
back and forth until the plan is solid — and turn it into the folder tree above.

The `ola-plan` skill automates that final translation step. At the end of a
planning session, tell the agent **"create the ola plan for this"**; the skill
reads the agreed plan and writes the `NN-description/` folders, deciding what
must run in sequence (separate numbered folders) and what is independent and may
run in parallel (tasks within one `PLAN.md`). The result is an agent folder you
can hand straight to `ola`.

The skill is **owned by this repository** at
[`.claude/skills/ola-plan/`](.claude/skills/ola-plan/SKILL.md). So it is
available from any planning session — Claude Code or OpenHands — it is symlinked
into the global skill directories. The same skill body backs every entry point:

```bash
# from the ola repo root — symlinks the repo-owned skill into both harnesses
make install-skill        # or run helper-scripts/install-ola-plan-skill.sh
```

This links `~/.claude/skills/ola-plan` (Claude Code) and
`~/.openhands/skills/ola-plan` (OpenHands) at the repo's copy, so editing the
skill here updates it everywhere.

## Setting up your agents
For OpenHands:
* Set your environment vars at the `.env` in the agent folder, including base URL, API key, model name and optional parameters. See example at `.env.example`.

For Claude Code:
* If using an Anthropic subscription, install Claude Code and login. This will store credentials in your keychain.
* If using an API key, define it in your `.env` in the agent folder.
* For a self-hosted Anthropic-compatible endpoint, set `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` in `.env` — the same vars used by OpenHands and Codex. cc translates them to `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` (and mirrors the model into `ANTHROPIC_SMALL_FAST_MODEL`). When self-hosted, OAuth credentials are not copied into the per-phase state dir. Add `LLM_SKIP_TLS_VERIFY=true` if the server uses a self-signed certificate. If the model has a small context window, set `LLM_MAX_OUTPUT_TOKENS` (e.g. `8192`) — cc passes it through as `CLAUDE_CODE_MAX_OUTPUT_TOKENS` to keep Claude Code's default 32000-token output request from overflowing the window.

For Codex:

* Setup your `~/.codex/config.toml` to use the desired model (similar to OpenHands).

## Parallel execution

Within a plan subfolder, ola can work on several `PLAN.md` tasks **at the same time**, each in its own isolated git worktree. Parallelism is controlled per-folder by a single file:

```bash
echo 3 > agent/02-implement/.ola/concurrency   # run up to 3 tasks at once
```

`<folder>/.ola/concurrency` holds one integer:

| Value | Meaning |
|-------|---------|
| missing / malformed | default cap of 2 — written to the file on the first tick |
| `N` (≥ 1) | run up to `N` tasks concurrently |
| `0` | pause new starts (in-flight tasks finish) |

When the file is absent, ola materializes it at the default (2) on the first scheduler tick, so the cap is always present on disk and the monitors always have a value to show. The file is **re-read on every scheduler tick**, so you can raise or lower the cap (or pause with `0`) while a run is in progress — no restart needed. There is no CLI flag for concurrency; the file is the knob.

### How a task runs

For each unchecked task in `PLAN.md`, a worker:

1. Creates a dedicated git worktree under `<folder>/.ola/worktrees/<task_id>/` (branch `ola/<folder>/<task_id>`), so concurrent tasks never step on each other.
2. Runs the agent against the `TASK-PROMPT.md` template (a sensible default is used if absent), with `{{task_text}}` / `{{task_id}}` substituted.
3. On success **and** a ticked checkbox, commits the worktree and cherry-picks it back onto the agent-folder branch (the `PLAN.md` checkbox tick is applied separately to avoid concurrent merge conflicts), then cleans up the worktree.
4. On failure or a *stagnant* attempt (agent reports success but never ticked the box), the task is requeued while `attempts < --max-attempts`; once the ceiling is reached it stays failed and its worktree is **retained for post-mortem**.

A per-folder circuit breaker halts the folder after 5 consecutive stagnant attempts, so a stuck agent can't spin forever.

### Blocked tasks and the janitor

A task agent that cannot complete its task because something **out of scope** is missing (a prerequisite, a credential, an undecided design) should not guess and should not tick its checkbox. ola provisions a small escape hatch into every task worktree — `.ola/bin/ola-blocked` — and the task prompt tells the agent to run it with a one-sentence reason and stop. A blocked task is terminal: it is never retried, regardless of `--max-attempts`, and it does not trip the stagnation circuit breaker.

Instead, the harness immediately dispatches a **janitor** — a sibling agent run (same backend/model) primed to unblock aggressively, while the folder's other tasks keep running. The janitor produces exactly one of two outcomes:

* **Unblock** (strongly preferred): it adds the missing prerequisite work as new unchecked checkboxes to the *current* folder's `PLAN.md` (picked up in the same run) and moves the blocked task into a new sibling **leftovers folder** — e.g. `01-init` spawns `01a-init-leftovers/PLAN.md` — which sorts right after the current folder and therefore runs before `02-…`.
* **Escalate** (last resort): when a human or an unobtainable resource is genuinely required, it creates a sibling **blockers folder** — e.g. `01b-init-blockers/BLOCKERS.md` — with the task, the worker's reason, and why it couldn't be unblocked. Folders without a `PLAN.md` are skipped, so the rest of the pipeline keeps advancing; ola lists all blockers in a "human attention needed" summary at the end of the run.

The full contract (folder naming, suffix allocation, tick-beats-marker) lives in [`src/ola/agents/CONTRACT.md`](./src/ola/agents/CONTRACT.md), which is also inlined into the janitor's prompt at runtime. Disable the janitor with `--no-janitor` if you want blocked tasks to simply stay blocked.

### State & events

* `<folder>/.ola/tasks.json` — the per-task state spine (`pending` / `running` / `complete` / `failed` / `blocked`, plus `attempts` and `last_error`), reconciled from `PLAN.md` on each run.
* `<folder>/.ola/events.jsonl` — a v2 event stream (`started` → `working`* → `complete`/`failed`) written for every attempt; this is the audit trail and the source [ola-top](./docs/ola-top.md) and [ola-dashboard](./docs/ola-dashboard.md) read per-task progress from. The wire format is documented in [`src/ola/events/SCHEMA.md`](./src/ola/events/SCHEMA.md).

Monitor a parallel run with [`ola-top`](./docs/ola-top.md), which renders a live per-task view with a `running N / cap M` badge.

## Docker Sandbox

Although you can run `ola` directly on your host, we strongly recommend running it inside a Docker sandbox (via [`sbx`](https://docs.docker.com/sandbox/)) for true filesystem and network isolation.

See **[docs/sandbox.md](./docs/sandbox.md)** for the full setup: building the template image, the `ola-sandbox`/`ola-monitor` shell helpers, network policies, and Laminar tracing.

## ola-top

`ola-top` is a `top`-like terminal dashboard for monitoring agent progress in real time — task completion, token usage, cache hit rates, and wall time, with per-iteration and per-task drill-down.

See **[docs/ola-top.md](./docs/ola-top.md)** for flags, keybindings, views, and the parallel per-task layout.

## ola-dashboard

`ola-dashboard` is the browser-based, visually rich sibling of `ola-top` — a work-item heatmap, hero metrics, an activity feed, and a live parallel-agents slider, aimed at demos as well as monitoring. It is a **view over the same `.ola/` files** (no collector, no background state): a thin stateless server re-reads the agent folder on each request and serves the built SPA.

```bash
make dashboard            # build the SPA once (npm install + vite build)
ola-dashboard -f ../agent
```

See **[docs/ola-dashboard.md](./docs/ola-dashboard.md)** for the routes, panels, and dev setup.

## Agents

**Claude Code** (`cc`) — calls `claude --dangerously-skip-permissions -p <prompt>` as a subprocess. When run via ola, `CLAUDE_CONFIG_DIR` is set to the phase's `.claude/` directory, giving each phase its own conversation history.

**Claude Code TUI** (`ct` / `claude-tui`) — *experimental.* Drives the **interactive** `claude` UI inside a pseudo-terminal instead of the headless `-p` stream: it spawns the TUI, suppresses the first-run onboarding and workspace-trust dialogs (by pre-seeding `.claude/.claude.json`), bracket-pastes the prompt, detects end-of-turn from the screen going idle, and tears the session down. Shares `cc`'s `.claude/` state dir and self-hosted `LLM_*` handling. **Metrics:** on teardown `ct` reads the per-task transcript the TUI flushes (`<state_dir>/projects/.../<session>.jsonl`) and recovers token counts, turns, models, and peak context (cost / cache-hit) — but **not** the streaming-only timings (TTFT, decode tok/sec), which are never persisted, and nothing for a session too short to flush. Completion is the ticked `PLAN.md` checkbox, the only signal the harness trusts. Use `cc` when you need live timing; use `ct` to exercise the real interactive UI. Requires a pty (works in the Docker sandbox; the host command-sandbox may deny pty allocation). See `src/ola/agents/claude_code_tui.py` for the full contract.

**OpenHands** (`oh`) — calls `openhands --headless --json --override-with-envs -f <task>` as a subprocess (one process per task, so tasks run truly in parallel — unlike the former in-process SDK backend, whose class-level lock serialized every LLM call). Requires the standalone `openhands` CLI on `PATH` (`uv tool install openhands`; it is **not** a Python dependency of ola) and `LLM_API_KEY` (optionally `LLM_MODEL`, `LLM_BASE_URL`) in the environment or a `.env` file. ola writes a per-task `agent_settings.json` (full LLM config) and sets `OPENHANDS_PERSISTENCE_DIR=<subfolder>/.openhands/`, where the conversation state and `base_state.json` (the post-hoc metrics source) are saved. Like `ct`, it has no streaming-only timings (TTFT, decode tok/sec). See `.claude/skills/openhands-cli/SKILL.md` for the full contract.

**Codex** (`cx` / `codex`) — calls `codex exec --json --ephemeral` as a subprocess and consumes its JSONL event stream. Reuses the same `LLM_*` vars as OpenHands (`LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, plus an optional `LLM_WIRE_API` override that defaults to `"responses"`). On each iteration, ola generates `<subfolder>/.codex/config.toml` pointing codex at the configured base URL via a `[model_providers.ola]` block (with `env_key = "LLM_API_KEY"`), and sets `CODEX_HOME=<subfolder>/.codex/` so the per-phase config is picked up. See `.claude/skills/codex/SKILL.md` for the underlying CLI + event-stream contract.
