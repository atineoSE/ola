# ola

Outer Loop of Agents — A harness to run long-horizon agentic loops

`ola` is a light harness that allows to run AI coding agents for long-horizon tasks. With a simple folder structure and a few markdown files, you can direct long-running coding tasks. 

The implementation follows the [Ralph Wiggum technique](https://ghuntley.com/ralph/): a way for the agent to iterate on fresh contexts as it works relentlessly against tasks in a plan file. The design is heavily influenced by [this presentation](https://youtu.be/5syeNjq2ZCU?si=a2RvALDjiXPfYqJn) from [Ray Myers](https://github.com/raymyers), Chief Architect at [OpenHands](https://openhands.dev).

There are 2 agents currently supported:
* [Claude Code](https://github.com/anthropics/claude-code)
* [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk)

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

Plan subfolders are processed in order.

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

## Docker Sandbox

Although you can run `ola` directly on your host machine, we strongly recommend using sandboxes for agent isolation. Sandboxes offer a structural barrier that prevents the agent from accessing anywhere in the filesystem and connecting to anywhere in the internet.

This repo provides a custom sandbox template and companion scripts to run `ola` inside of a docker sandox, using [`sbx`](https://docs.docker.com/sandbox/) (microVM). This provides several layers of isolation, including a network proxy to control outbound traffic. See [here](https://docs.docker.com/ai/sandboxes/security/isolation/) for more details on isolation proporties of docker sandboxes. 

Note the isolation provided by docker sandboxes is much more strict that the Claude Code [sandbox feature](https://code.claude.com/docs/en/sandboxing), which doesn't offer true filesystem or network isolation, especially when combined with `--dangerously-skip-permissions`.

### Prerequisites

* Install sbx, login, and set your default policy. The recommended policy is "balanced", which defaults to deny traffic except for approved service providers and package managers. See [here](https://docs.docker.com/ai/sandboxes/security/policy/#network-policies) for more information about policies.
* Add an `allowlist.txt` file in the agent folder to allow traffic to specific domains. They apply globally to all local sandboxes and include all subdomains.

### Build and push the template image

The template extends `docker/sandbox-templates:shell` and must be pushed to an OCI registry — sbx normally pulls templates from a registry directly and does not use the local Docker daemon's image store.

```bash
docker build --no-cache -f docker/Dockerfile -t ghcr.io/$(whoami)/ola:latest --push .
```

### Dev flow (local image, no registry push)

When iterating on ola itself, build a local image and load it into sbx's image store, then point `ola-sandbox` at it via `OLA_SBX_IMAGE`.

```bash
make sandbox-dev                                  # builds ola:dev and loads it into sbx
OLA_SBX_IMAGE=ola:dev ola-sandbox my-sandbox      # creates sandbox from local image
```

Sandboxes are ephemeral — to pick up a new build, just `sbx rm -f my-sandbox` and recreate.

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

### Create a sandbox

From the `project/src` directory:

```bash
ola-sandbox my-sandbox
```

This will:
1. Extract Claude OAuth credentials from macOS Keychain (`cc-credentials`)
2. Apply project-specific network allowlist from `agent/allowlist.txt` (additive to default policy)
3. Create a sandbox with the project directory (parent of `src/`) as workspace — both `src/` and `agent/` are writable
4. Copy credentials into the sandbox and set the shell to land in `src/`

Running `ola-sandbox my-sandbox` again will reconnect to the existing sandbox.

Inside the sandbox:

```bash
ola -a cc -l 5
```

### Manual usage

If you prefer not to use the helper:

```bash
cd project
sbx create shell --name my-sandbox --template ghcr.io/$(whoami)/ola:latest .
sbx run my-sandbox
```

### Network policy

The `balanced` policy provides deny-by-default with allowlists for AI APIs, package managers, code hosts, and registries. To manage policies:

```bash
sbx policy ls --type network          # show active rules
sbx policy allow network "example.com,*.example.com"  # add allow rule
sbx policy log                        # view blocked requests
```

Project-specific domains can be added to `agent/allowlist.txt` (one domain per line). The `ola-sandbox` helper applies these automatically on sandbox creation.

### Laminar tracing (OpenHands only)

Set `LMNR_PROJECT_API_KEY` and `LMNR_BASE_URL` in `.env` to enable trace export to [Laminar](https://www.lmnr.ai) when using the OpenHands agent (`-a oh`). Traces are exported over HTTP (OTLP/HTTP) on the port specified by `LMNR_HTTP_PORT` (default `8000`).

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
