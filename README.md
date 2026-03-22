# ola

Outer Loop of Agents — runs coding agents in a loop over structured plan folders.

## Install

```bash
uv tool install .
```

## Usage

```bash
ola -p <plan-folder> [-a cc|oh] [-m MODEL] [-l LIMIT] [-v]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-p, --plan` | Path to the plan folder (required) | — |
| `-a, --agent` | Agent: `cc`/`claude-code` or `oh`/`openhands` | `cc` |
| `-m, --model` | Model name | Agent default |
| `-l, --limit` | Max iterations per subfolder | No limit |
| `-v, --verbose` | Debug logging | Off |

## Plan folder structure

```
my-plan/
  01-setup/
    SEED-PROMPT.md    # Optional: runs once to generate PLAN.md
    LOOP-PROMPT.md    # Required: prompt used each iteration
    PLAN.md           # Optional: markdown todo list
  02-implement/
    LOOP-PROMPT.md
    PLAN.md
```

Subfolders are processed in order. For each subfolder:

1. If `SEED-PROMPT.md` exists and `PLAN.md` does not, the seed prompt runs first to populate the plan.
2. While `PLAN.md` has unchecked tasks (`- [ ]`), the agent runs `LOOP-PROMPT.md` repeatedly.
3. Stops when all tasks are checked or the iteration limit is reached.

## Docker Sandbox

Run `ola` inside a [Docker sandbox](https://docs.docker.com/sandbox/) (microVM-based isolation).

### Build the template image

```bash
docker build -f docker/Dockerfile -t ola:latest .
```

### Shell helper

Source the helper function in your `.bashrc` or `.zshrc`:

```bash
source /path/to/ola/docker/ola-sandbox.sh
```

### Run a sandbox

The expected directory layout is:

```
experiment/
  code/   # your working directory
  plan/   # ola plan folder (sibling)
```

From the `code` directory:

```bash
ola-sandbox my-sandbox
```

This will:
1. Copy `~/.claude/.credentials.json` into the workspace for Claude auth
2. Create a sandbox with `code/` as primary workspace and `plan/` mounted alongside
3. Clean up the credentials file from the workspace on exit

Inside the sandbox:

```bash
ola -p ../plan -a cc -l 5
```

To reconnect to an existing sandbox:

```bash
docker sandbox run my-sandbox
```

### Manual usage

If you prefer not to use the helper:

```bash
cp ~/.claude/.credentials.json .
docker sandbox run --name my-sandbox -t ola:latest shell . ../plan
# credentials are auto-moved to ~/.claude/ on shell login
```

Place a `.env` file in the workspace for OpenHands env vars (`LLM_API_KEY`, etc.).

## Agents

**Claude Code** (`cc`) — calls `claude --dangerously-skip-permissions -p <prompt>` as a subprocess.

**OpenHands** (`oh`) — uses the OpenHands SDK (`LLM` + `Conversation`). Requires `LLM_API_KEY` (and optionally `LLM_MODEL`, `LLM_BASE_URL`) set in the environment or a `.env` file. SDK logs and conversation trajectories are saved to `<subfolder>/logs/` and `<subfolder>/trajectories/`.
