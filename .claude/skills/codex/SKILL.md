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

### Top-level shape

```json
{
  "timestamp": "2026-05-13T09:33:52.589Z",
  "type": "event_msg | response_item | session_meta | turn_context",
  "payload": { "...depends on type..." }
}
```

### Event types that carry the metadata a harness usually wants

1. **`session_meta`** — run/session info: id, cwd, CLI version, model_provider, git branch/commit, etc. Emit-once, near the start.
2. **`turn_context`** — effective runtime config for the turn: model, approval policy, sandbox policy, cwd, date/timezone.
3. **`event_msg`** with `payload.type == "token_count"` — usage: `input_tokens`, `output_tokens`, `reasoning_output_tokens`, totals, context window, rate-limit info.
4. **`event_msg`** with `payload.type == "task_complete"` — completion: `turn_id`, often `last_agent_message`.
5. **`response_item`** — model/user/developer messages and tool-call records: `message`, `function_call`, `function_call_output`, etc.

### Practical extraction with `jq`

Pull just the metadata-bearing events:

```bash
codex exec --json "your prompt" | jq -c '
  select(
    .type == "session_meta" or
    .type == "turn_context" or
    (.type == "event_msg" and (.payload.type == "token_count" or .payload.type == "task_complete"))
  )'
```

### Minimal Python consumer pattern

```python
import json, subprocess

proc = subprocess.Popen(
    ["codex", "exec", "--json", "--ephemeral", "-C", ".", prompt],
    stdout=subprocess.PIPE, text=True,
)

last_message = None
usage = None
session_id = None

for line in proc.stdout:
    line = line.strip()
    if not line:
        continue
    evt = json.loads(line)
    t = evt.get("type")
    payload = evt.get("payload", {})

    if t == "session_meta":
        session_id = payload.get("id")
    elif t == "event_msg" and payload.get("type") == "token_count":
        usage = payload
    elif t == "event_msg" and payload.get("type") == "task_complete":
        last_message = payload.get("last_agent_message")

proc.wait()
```

Stream events as they arrive — don't buffer to the end. That gives the harness live progress for monitors/logs and avoids losing partial output if the run is killed.

---

## 4) Integrating Codex as an `ola` agent — checklist

When wiring this into `ola` as a third agent alongside `cc` and `oh`:

- Add a new agent class under `src/ola/agents/` (e.g. `codex.py`) following the same `base.py` shape as `claude_code.py` / `openhands.py`.
- Build the command as `codex exec --json --ephemeral -C <workdir> [-p <profile> | -m <model>] <prompt>`.
- Set provider creds in the subprocess env (the var named by `env_key` in `config.toml`), not on the command line.
- Stream stdout line-by-line, decode each line as JSON, and surface `token_count` / `task_complete` / `response_item` events to whatever ola already uses for stats and monitoring.
- If a `-o <file>` final-message file is desired (mirrors how the other agents capture their answer), write it to the per-iteration workdir so the loop driver can pick it up.
- Expose a CLI alias (e.g. `-a codex` or `-a cx`) in `cli.py` parallel to `cc` / `oh`.
