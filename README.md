# ola

Outer Loop of Agents — runs coding agents in a loop over structured plan folders.

## Install

```bash
uv tool install .
```

## Usage

```bash
ola [-f <agent-folder>] [-a cc|oh] [-m MODEL] [-l LIMIT] [-v]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-f, --agent-folder` | Path to the agent folder | `../agent` |
| `-a, --agent` | Agent: `cc`/`claude-code` or `oh`/`openhands` | `cc` |
| `-m, --model` | Model name | Agent default |
| `-l, --limit` | Max iterations per subfolder | No limit |
| `-v, --verbose` | Debug logging | Off |

## Agent folder structure

```
my-agent/
  01-setup/
    SEED-PROMPT.md    # Optional: runs once to generate PLAN.md
    LOOP-PROMPT.md    # Required: prompt used each iteration
    PLAN.md           # Optional: markdown todo list
    .claude/          # Claude Code config dir (auto-created by ola)
      projects/...    # conversation history auto-created by claude
    .openhands/       # OpenHands state dir (auto-created by ola)
      logs/
      trajectories/
  02-implement/
    LOOP-PROMPT.md
    PLAN.md
    .claude/
    .openhands/
```

Subfolders are processed in order. For each subfolder:

1. If `SEED-PROMPT.md` exists and `PLAN.md` does not, the seed prompt runs first to populate the plan.
2. While `PLAN.md` has unchecked tasks (`- [ ]`), the agent runs `LOOP-PROMPT.md` repeatedly.
3. Stops when all tasks are checked or the iteration limit is reached.

Each agent gets a per-phase state directory (`.claude/` or `.openhands/`) inside the subfolder. For Claude Code, `CLAUDE_CONFIG_DIR` is set to `.claude/`, giving each phase its own conversation history that persists across sandbox sessions. For OpenHands, logs and trajectories are written to `.openhands/logs/` and `.openhands/trajectories/`.

## Docker Sandbox

Run `ola` inside a Docker sandbox using [`sbx`](https://docs.docker.com/sandbox/) (microVM-based isolation).

### Prerequisites

Set up credentials and network policy once:

```bash
# Authenticate Claude on the host (creates ~/.claude/.credentials.json via OAuth)
claude

# Set default network policy to balanced (deny-all + common dev allowlist)
sbx policy set-default balanced
```

> **Note:** We use a Claude subscription (OAuth), not an API key. The `ola-sandbox` helper copies `~/.claude/.credentials.json` from your host into the sandbox on each creation/reconnection.

### Build the template image

Use `--no-cache` to ensure the latest versions of Claude Code, OpenHands, and ola are installed:

```bash
docker build --no-cache -f docker/Dockerfile -t docker.io/ola/ola-sbx:latest --push .
```

### Shell helpers

Symlink `ola.sh` to your home directory and source it from `.zshrc`:

```bash
ln -sf /path/to/ola/ola.sh ~/.ola.sh
```

Add to your `.zshrc`:

```bash
[ -f ~/.ola.sh ] && source ~/.ola.sh
```

This provides **`ola-sandbox`** — creates or reconnects to a Docker sandbox.

### Run a sandbox

The expected directory layout is:

```
experiment/
  code/    # your working directory
  agent/   # ola agent folder (sibling)
```

From the `code` directory:

```bash
ola-sandbox my-sandbox
```

This will:
1. Apply project-specific network allowlist from `agent/whitelist.txt` (additive to balanced policy)
2. Create a sandbox with `code/` as primary workspace and `agent/` mounted read-only
3. Credentials are copied from host `~/.claude/.credentials.json` into the sandbox (OAuth token)

Running `ola-sandbox my-sandbox` again will reconnect to the existing sandbox.

Inside the sandbox:

```bash
ola -a cc -l 5
```

### Manual usage

If you prefer not to use the helper:

```bash
sbx run --name my-sandbox --template docker.io/ola/ola-sbx:latest claude . ../agent:ro
```

Place a `.env` file in the workspace for OpenHands env vars (`LLM_API_KEY`, etc.).

### Network policy

The `balanced` policy provides deny-by-default with allowlists for AI APIs, package managers, code hosts, and registries. To manage policies:

```bash
sbx policy ls --type network          # show active rules
sbx policy allow network "example.com,*.example.com"  # add allow rule
sbx policy log                        # view blocked requests
```

Project-specific domains can be added to `agent/whitelist.txt` (one domain per line). The `ola-sandbox` helper applies these automatically on sandbox creation.

### Laminar tracing

Set `LMNR_PROJECT_API_KEY` and `LMNR_BASE_URL` in `.env` to enable trace export to [Laminar](https://www.lmnr.ai). Traces are exported over HTTP (OTLP/HTTP) on the port specified by `LMNR_HTTP_PORT` (default `8000`).

> **Note:** gRPC export (the default in the Laminar SDK) does not work inside Docker sandboxes. The sbx proxy downgrades HTTP/2 to HTTP/1.x, which breaks gRPC. ola uses `force_http=True` to avoid this entirely.

## ola-top

A `top`-like terminal dashboard for monitoring agent progress in real time. Shows task completion, token usage, cache hit rates, and wall time for each phase — with per-iteration drill-down.

```bash
ola-top [-f <agent-folder>] [-r <refresh-seconds>]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-f, --agent-folder` | Path to the agent folder | `../agent` |
| `-r, --refresh` | Refresh interval in seconds | `2` |

**Keybindings:** `↑`/`↓` navigate rows, `Enter` expands/collapses a phase to show per-iteration stats, `q` quits.

Example output:

```
 ola-top — /Users/you/experiment/agent             03:42:15 PM

 # │ Folder              │ Tasks │   Input │  Output │ Cache% │  Time
 1 │ 01-setup            │   5/5 │  120.4k │   45.2k │  82.3% │  3m12s
 2 │ 02-implement        │  3/10 │   88.1k │   32.7k │  76.1% │  2m45s

 q: quit  ↑↓: navigate  Enter: expand/collapse
```

## Agents

**Claude Code** (`cc`) — calls `claude --dangerously-skip-permissions -p <prompt>` as a subprocess. When run via ola, `CLAUDE_CONFIG_DIR` is set to the phase's `.claude/` directory, giving each phase its own conversation history.

**OpenHands** (`oh`) — uses the OpenHands SDK (`LLM` + `Conversation`). Requires `LLM_API_KEY` (and optionally `LLM_MODEL`, `LLM_BASE_URL`) set in the environment or a `.env` file. SDK logs and conversation trajectories are saved to `<subfolder>/.openhands/logs/` and `<subfolder>/.openhands/trajectories/`.
