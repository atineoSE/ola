---
name: codex
description: How to drive the Codex CLI agent headlessly against a replaceable (custom/remote) model provider, and how to parse its JSONL event stream. Use when integrating Codex as an agent backend (e.g. a third ola agent alongside Claude Code and OpenHands).
---

# Codex CLI — Headless Usage with Replaceable Providers

This skill describes the two things needed to drive the [Codex](https://github.com/openai/codex) CLI as a back-end agent inside another harness (such as `ola`):

1. Pointing Codex at an arbitrary remote model provider via `~/.codex/config.toml` (so the same binary can talk to OpenAI, a self-hosted vLLM endpoint, an internal gateway, etc.).
2. Running Codex non-interactively with `codex exec`, with `--json` for a machine-parseable JSONL event stream.

---

## 1) Configure a custom remote model provider

Codex resolves the active model + provider from `~/.codex/config.toml`. Define one or more providers under `[model_providers.<name>]` and select one with the top-level `model_provider` key (or via a profile).

```toml
# ~/.codex/config.toml

model_provider = "myremote"
model          = "your-model-name"

[model_providers.myremote]
name     = "myremote"
base_url = "https://your-host.example.com/v1"
env_key  = "MYREMOTE_API_KEY"   # name of the env var that holds the API key
wire_api = "responses"          # "responses" or "chat" — match your endpoint
```

Then export the key in the shell that launches Codex:

```bash
export MYREMOTE_API_KEY=...
```

### Profiles for switching models/providers

Multiple `[profiles.*]` blocks let you keep several (provider, model) pairs side by side and pick one at invocation time:

```toml
[profiles.remote-fast]
model_provider = "myremote"
model          = "your-fast-model"

[profiles.remote-smart]
model_provider = "myremote"
model          = "your-smart-model"
```

Select a profile with `-p`:

```bash
codex -p remote-fast
codex exec -p remote-smart "..."
```

### Provider-config keys cheat-sheet

| Key        | Meaning                                                                                   |
| ---------- | ----------------------------------------------------------------------------------------- |
| `name`     | Human-readable provider name.                                                             |
| `base_url` | Endpoint root (typically ends in `/v1`).                                                  |
| `env_key`  | **Name** of the environment variable Codex should read the API key from (not the key itself). |
| `wire_api` | `"responses"` for OpenAI Responses-style endpoints; `"chat"` for Chat Completions style.  |

### Notes for harness authors

- Don't hard-code keys into `config.toml` — only the env-var **name** belongs there. The harness should set the actual secret in the child process environment.
- The `-m <model>` flag overrides the model for a single run without touching config.
- `model_provider` at the top level is the default; `-p <profile>` overrides it.

---

## 2) Headless / non-interactive runs with `codex exec`

`codex exec` runs one task end-to-end without a TUI. This is the entry point a harness should use.

```bash
codex exec "Summarize this repo and propose a refactor plan"
```

### Most useful flags

| Flag             | Purpose                                                                       |
| ---------------- | ----------------------------------------------------------------------------- |
| `--json`         | Emit a JSONL event stream on stdout (one JSON object per line).               |
| `-o <file>`      | Write the final assistant message to `<file>`.                                |
| `--ephemeral`    | Don't persist any session files on disk.                                      |
| `-C <dir>`       | Run as if cwd were `<dir>`.                                                   |
| `-p <profile>`   | Use a named profile from `config.toml`.                                       |
| `-m <model>`     | Override the model for this run.                                              |

### CI-friendly example

```bash
codex exec --json --ephemeral -C . -o final.txt \
  "Run tests, explain failures, and suggest fixes"
```

- `--json` → consume the stream programmatically.
- `--ephemeral` → no session files leak between runs (important for parallel/loop harnesses).
- `-C .` → make cwd explicit so a wrapper script can change it per task.
- `-o final.txt` → grab the final assistant message without re-parsing the stream.

---

## 3) Parsing the `--json` event stream (JSONL)

Each line of `codex exec --json` is one JSON object. Parse line-by-line; don't try to load the whole stream as one document.

> Format documented here is the **v0.130.0** stream. Older codex builds (≤ ~0.110) emitted a `session_meta` / `turn_context` / `event_msg` / `response_item` envelope under a `payload` key; that format is gone in current builds.

### Event types that carry the metadata a harness usually wants

1. **`thread.started`** — `{ type, thread_id }`. Emitted once at the start.
2. **`turn.started`** — no payload of interest. Marks the beginning of an LLM turn (may repeat in multi-turn sessions).
3. **`item.started`** / **`item.completed`** — `{ type, item: { id, type, ... } }`. The `item.type` discriminator selects the shape:
   - `agent_message` — `{ id, type, text }`. The model's reply for this turn. Track the latest one as the run's `last_agent_message`.
   - `command_execution` — `{ id, type, command, aggregated_output, exit_code, status }`. A shell command the agent ran.
   - Other tool/item types (reasoning, file edits, etc.) — handle defensively; the set grows as new tools land.
4. **`turn.completed`** — `{ type, usage: { input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens } }`. Per-turn usage. **Sum across turns** for cumulative totals; the largest single-turn `input_tokens` is your context-window high-water mark.
5. **`turn.failed`** — `{ type, error: { message } }`. Turn-level failure (e.g. provider rejected the request).
6. **`error`** — `{ type, message }`. Stream-level error.

A run is **successful** when at least one `turn.completed` is seen and no `turn.failed` / `error` event appears.

The effective model name is **not** surfaced in the stream — harnesses should record the model they configured (top-level `model` in `config.toml` or `-m <model>` override).

### Practical extraction with `jq`

Pull just the metadata-bearing events:

```bash
codex exec --json "your prompt" | jq -c '
  select(
    .type == "thread.started" or
    .type == "turn.completed" or
    .type == "turn.failed" or
    (.type == "item.completed" and .item.type == "agent_message")
  )'
```

### Minimal Python consumer pattern

```python
import json, subprocess

proc = subprocess.Popen(
    ["codex", "exec", "--json", "--ephemeral",
     "--dangerously-bypass-approvals-and-sandbox", "-C", ".", prompt],
    stdout=subprocess.PIPE, text=True,
)

thread_id = None
last_message = None
input_tokens = output_tokens = cached = 0
turn_completed = False
turn_error = None

for line in proc.stdout:
    line = line.strip()
    if not line:
        continue
    evt = json.loads(line)
    t = evt.get("type")

    if t == "thread.started":
        thread_id = evt.get("thread_id")
    elif t == "item.completed":
        item = evt.get("item") or {}
        if item.get("type") == "agent_message":
            last_message = item.get("text")
    elif t == "turn.completed":
        turn_completed = True
        u = evt.get("usage") or {}
        input_tokens += u.get("input_tokens", 0) or 0
        output_tokens += u.get("output_tokens", 0) or 0
        cached += u.get("cached_input_tokens", 0) or 0
    elif t in ("turn.failed", "error"):
        turn_error = (evt.get("error") or {}).get("message") or evt.get("message")

proc.wait()
success = turn_completed and turn_error is None
```

Stream events as they arrive — don't buffer to the end. That gives the harness live progress for monitors/logs and avoids losing partial output if the run is killed.

### Sandboxing flag

Without `--dangerously-bypass-approvals-and-sandbox`, `codex exec` runs with a read-only sandbox and will refuse to edit files. Pass that flag when you are *already* running inside an externally-isolated environment (e.g. an ola docker sandbox). For unsandboxed local runs, prefer `-s workspace-write` and let codex prompt for approvals.

---

## 4) Integrating Codex as an `ola` agent — checklist

When wiring this into `ola` as a third agent alongside `cc` and `oh`:

- Add a new agent class under `src/ola/agents/` (e.g. `codex.py`) following the same `base.py` shape as `claude_code.py` / `openhands.py`.
- Build the command as `codex exec --json --ephemeral -C <workdir> [-p <profile> | -m <model>] <prompt>`.
- Set provider creds in the subprocess env (the var named by `env_key` in `config.toml`), not on the command line.
- Stream stdout line-by-line, decode each line as JSON, and surface `token_count` / `task_complete` / `response_item` events to whatever ola already uses for stats and monitoring.
- If a `-o <file>` final-message file is desired (mirrors how the other agents capture their answer), write it to the per-iteration workdir so the loop driver can pick it up.
- Expose a CLI alias (e.g. `-a codex` or `-a cx`) in `cli.py` parallel to `cc` / `oh`.
