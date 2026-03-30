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
      .credentials.json
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

Run `ola` inside a [Docker sandbox](https://docs.docker.com/sandbox/) (microVM-based isolation).

### Build the template image

Use `--no-cache` to ensure the latest versions of Claude Code, OpenHands, and ola are installed:

```bash
docker build --no-cache -f docker/Dockerfile -t ola:latest .
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

This provides two functions:

- **`cc-credentials`** — restores `~/.claude/.credentials.json` from the macOS Keychain
- **`ola-sandbox`** — creates or reconnects to a Docker sandbox

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
1. Restore `~/.claude/.credentials.json` from the Keychain if missing (via `cc-credentials`)
2. Copy credentials into the workspace for the sandbox to pick up
3. Create a sandbox with `code/` as primary workspace and `agent/` mounted alongside
4. Clean up the credentials file from the workspace on exit

Running `ola-sandbox my-sandbox` again will reconnect to the existing sandbox.

Inside the sandbox:

```bash
ola -a cc -l 5
```

### Manual usage

If you prefer not to use the helper:

```bash
cp ~/.claude/.credentials.json .
docker sandbox run --name my-sandbox -t ola:latest shell . ../agent
# credentials are auto-moved to ~/.claude/ on shell login
```

Place a `.env` file in the workspace for OpenHands env vars (`LLM_API_KEY`, etc.).

### Laminar tracing

Set `LMNR_PROJECT_API_KEY` and `LMNR_BASE_URL` in `.env` to enable trace export to [Laminar](https://www.lmnr.ai). Traces are exported over HTTP (OTLP/HTTP) on the port specified by `LMNR_HTTP_PORT` (default `8000`).

> **Note:** gRPC export (the default in the Laminar SDK) does not work inside Docker sandboxes. The sandbox MITM proxy downgrades HTTP/2 to HTTP/1.x, which breaks gRPC. `--bypass-host` is not a workaround because bypassed connections lose `host.docker.internal` routing and hit the default CIDR block on `127.0.0.0/8`. ola uses `force_http=True` to avoid this entirely.

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
