# OLA: Outer Loop of Agents — A harness to run long-horizon agentic loops

`ola` is a light harness that allows to run AI coding agents for long-horizon tasks. With a simple folder structure and a few markdown files, you can direct long-running coding tasks. 

The implementation follows the [Ralph Wiggum technique](https://ghuntley.com/ralph/): a way for the agent to iterate on fresh contexts as it works relentlessly against tasks in a plan file. The design is heavily influenced by [this presentation](https://youtu.be/5syeNjq2ZCU?si=a2RvALDjiXPfYqJn) from [Ray Myers](https://github.com/raymyers), Chief Architect at [OpenHands](https://openhands.dev).

There are 3 agents currently supported:
* [Claude Code](https://github.com/anthropics/claude-code)
* [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk)
* [Codex](https://github.com/openai/codex)

## Install

```bash
uv tool install .
```

## Usage

```bash
ola [-f <agent-folder>] [-a cc|oh|codex] [-m MODEL] [-l LIMIT] [--max-attempts N] [-v]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-f, --agent-folder` | Path to the agent folder | `../agent` |
| `-a, --agent` | Agent: `cc`/`claude-code`, `oh`/`openhands`, or `cx`/`codex` | `cc` |
| `-m, --model` | Model name | Agent default |
| `-l, --limit` | Max iterations per subfolder (ignored in parallel mode) | No limit |
| `--max-attempts` | Retry ceiling for failed/stagnant tasks | `0` (no retries) |
| `-v, --verbose` | Debug logging | Off |

## Folder structure

```
project/
  src/                  # your source code (must be a git repo)
  agent/                # ola agent folder (git repo created by ola if missing)
    .env                # LLM_BASE_URL, LMNR_BASE_URL, etc. (gitignored)
    allowlist.txt       # Optional: Domain list to allow inside the sandbox
    01-setup/           # Plan subfolder
      SEED-PROMPT.md    # Optional: runs once to generate PLAN.md
      LOOP-PROMPT.md    # Optional: prompt used each iteration
      TASK-PROMPT.md    # Optional: per-task prompt template (parallel mode)
      PLAN.md           # Optional: markdown todo list
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
      LOOP-PROMPT.md
      PLAN.md
      .claude/
      .openhands/
      .codex/
      .ola/
```

### Ordering: folders are sequential, tasks are parallel

The plan structure carries an implicit contract with two levels:

* **Tasks within one `PLAN.md` are independent and parallel-safe.** ola may run them concurrently (see [Parallel execution](#parallel-execution)) and gives no guarantee about their relative order. Never write a task that relies on a sibling task in the same plan having run first — even at the default concurrency cap of 1, order is not part of the contract.
* **Ordering comes from folders.** Plan subfolders are processed strictly in name order (`01-…`, `02-…`, …), and a folder must fully complete before the next one starts.

To express dependent work, split it into indexed `NN-description/` folders — one folder per dependency stage — each with its own `PLAN.md` of mutually independent tasks.

A plan subfolder must contain only ONE of these two files:
* `SEED-PROMPT.md`: this will create the `PLAN.md` file, which will drive the loop, or
* `PLAN.md`: it already contains the plan and thus no seed is needed.

While `PLAN.md` has unchecked tasks (`- [ ]`), the agent runs `LOOP-PROMPT.md` repeatedly. The agent stops when all tasks are checked or the iteration limit is reached.

The `LOOP-PROMT.md` is optional and it will be initialized to a sensible default if missing. However, it is recommended that this file is manually created, since it's key to drive the agent through long-running tasks.

The agent folder must be its own git repository (ola initialises one if missing). ola commits to this repo after each seed phase and loop iteration, tracking plan progress independently from your source code.

Each agent gets a per-phase state directory (`.claude/` or `.openhands/`) inside each plan subfolder. For Claude Code, `CLAUDE_CONFIG_DIR` is set to `.claude/`, giving each phase its own conversation history that persists across sandbox sessions. For OpenHands, logs and trajectories are written to `.openhands/logs/` and `.openhands/trajectories/`.

## Setting up your agents
For OpenHands:
* Set your environment vars at the `.env` in the agent folder, including base URL, API key, model name and optional parameters. See example at `.env.example`.

For Claude Code:
* If using an Anthropic subscription, install Claude Code and login. This will store credentials in your keychain.
* If using an API key, define it in your `.env` in the agent folder.
* For a self-hosted Anthropic-compatible endpoint, set `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` in `.env` — the same vars used by OpenHands and Codex. cc translates them to `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` (and mirrors the model into `ANTHROPIC_SMALL_FAST_MODEL`). When self-hosted, OAuth credentials are not copied into the per-phase state dir. Add `LLM_SKIP_TLS_VERIFY=true` if the server uses a self-signed certificate.

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
| missing / malformed | sequential (cap 1) — the default |
| `N` (≥ 1) | run up to `N` tasks concurrently |
| `0` | pause new starts (in-flight tasks finish) |

The file is **re-read on every scheduler tick**, so you can raise or lower the cap (or pause with `0`) while a run is in progress — no restart needed. There is no CLI flag for concurrency; the file is the knob.

### How a task runs

For each unchecked task in `PLAN.md`, a worker:

1. Creates a dedicated git worktree under `<folder>/.ola/worktrees/<task_id>/` (branch `ola/<folder>/<task_id>`), so concurrent tasks never step on each other.
2. Runs the agent against the `TASK-PROMPT.md` template (a sensible default is used if absent), with `{{task_text}}` / `{{task_id}}` substituted.
3. On success **and** a ticked checkbox, commits the worktree and cherry-picks it back onto the agent-folder branch (the `PLAN.md` checkbox tick is applied separately to avoid concurrent merge conflicts), then cleans up the worktree.
4. On failure or a *stagnant* attempt (agent reports success but never ticked the box), the task is requeued while `attempts < --max-attempts`; once the ceiling is reached it stays failed and its worktree is **retained for post-mortem**.

A per-folder circuit breaker halts the folder after 5 consecutive stagnant attempts, so a stuck agent can't spin forever.

### State & events

* `<folder>/.ola/tasks.json` — the per-task state spine (`pending` / `running` / `complete` / `failed`, plus `attempts` and `last_error`), reconciled from `PLAN.md` on each run.
* `<folder>/.ola/events.jsonl` — a v2 event stream (`started` → `working`* → `complete`/`failed`) written for every attempt; this is the audit trail and the source [ola-top](./docs/ola-top.md) reads per-task progress from. The wire format is documented in [`src/ola/events/SCHEMA.md`](./src/ola/events/SCHEMA.md). Set `OLA_COLLECTOR_URL` to additionally POST events to a remote collector.

Monitor a parallel run with [`ola-top`](./docs/ola-top.md), which renders a live per-task view with a `running N / cap M` badge.

## Docker Sandbox

Although you can run `ola` directly on your host, we strongly recommend running it inside a Docker sandbox (via [`sbx`](https://docs.docker.com/sandbox/)) for true filesystem and network isolation.

See **[docs/sandbox.md](./docs/sandbox.md)** for the full setup: building the template image, the `ola-sandbox` shell helper, network policies, and Laminar tracing.

## ola-top

`ola-top` is a `top`-like terminal dashboard for monitoring agent progress in real time — task completion, token usage, cache hit rates, and wall time, with per-iteration and per-task drill-down.

See **[docs/ola-top.md](./docs/ola-top.md)** for flags, keybindings, views, and the parallel per-task layout.

## Agents

**Claude Code** (`cc`) — calls `claude --dangerously-skip-permissions -p <prompt>` as a subprocess. When run via ola, `CLAUDE_CONFIG_DIR` is set to the phase's `.claude/` directory, giving each phase its own conversation history.

**OpenHands** (`oh`) — uses the OpenHands SDK (`LLM` + `Conversation`). Requires `LLM_API_KEY` (and optionally `LLM_MODEL`, `LLM_BASE_URL`) set in the environment or a `.env` file. SDK logs and conversation trajectories are saved to `<subfolder>/.openhands/logs/` and `<subfolder>/.openhands/trajectories/`.

**Codex** (`cx` / `codex`) — calls `codex exec --json --ephemeral` as a subprocess and consumes its JSONL event stream. Reuses the same `LLM_*` vars as OpenHands (`LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, plus an optional `LLM_WIRE_API` override that defaults to `"responses"`). On each iteration, ola generates `<subfolder>/.codex/config.toml` pointing codex at the configured base URL via a `[model_providers.ola]` block (with `env_key = "LLM_API_KEY"`), and sets `CODEX_HOME=<subfolder>/.codex/` so the per-phase config is picked up. See `.claude/skills/codex/SKILL.md` for the underlying CLI + event-stream contract.
