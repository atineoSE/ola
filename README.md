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

### Create and run a sandbox

```bash
# Create a sandbox with your plan folder synced as workspace
docker sandbox create --image ola:latest my-sandbox ~/my-plan

# Run the sandbox
docker sandbox run my-sandbox

# Inside the sandbox, ola and claude are ready to use
ola -p ~/my-plan -a oh -l 5
```

Place a `.env` file in the workspace directory — it syncs automatically into the sandbox.

## Agents

**Claude Code** (`cc`) — calls `claude --dangerously-skip-permissions -p <prompt>` as a subprocess.

**OpenHands** (`oh`) — uses the OpenHands SDK (`LLM` + `Conversation`). Requires `LLM_API_KEY` (and optionally `LLM_MODEL`, `LLM_BASE_URL`) set in the environment or a `.env` file. SDK logs and conversation trajectories are saved to `<subfolder>/logs/` and `<subfolder>/trajectories/`.
