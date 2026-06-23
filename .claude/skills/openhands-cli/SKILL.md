---
name: openhands-cli
description: How to drive the OpenHands CLI headlessly as ola's `oh` backend — subprocess invocation, the agent_settings.json config it loads, the --JSON Event- stream format, and post-hoc metrics. Load whenever changing the `oh` backend.
version: 2.0.0
---

# Driving the OpenHands CLI as the `oh` backend

ola's `oh` backend (`src/ola/agents/openhands.py`) runs the standalone
**`openhands` CLI** in a subprocess, one process per task — the same shape as
`cc`/`cx`. This is a deliberate redesign away from the in-process OpenHands
**SDK**: the SDK serializes every LLM call on a class-level lock (see the last
section), so an in-process backend can never run tasks truly in parallel. A
subprocess-per-task has its own `litellm` globals and its own lock, so the
fan-out is real.

The CLI is installed separately (`uv tool install openhands`, see
`docker/Dockerfile`); it is **not** a Python dependency of ola. Outside the
sandbox it must be on `PATH`.

## Headless invocation

```bash
openhands --headless --json --override-with-envs -f <task-file>
```

- `--headless` — no UI, **auto-approves actions**, auto-sets
  `--exit-without-confirmation`. Requires `--task`/`-t` or `--file`/`-f`. Runs
  to completion then exits, so **process exit is the completion signal**.
- `--json` — streams events to stdout (format below). Must be used with
  `--headless`.
- `--override-with-envs` — applies `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`
  from the environment over whatever was loaded from disk. ola passes these so
  the live identity always wins (the sandbox substrate IP/key rotates between
  runs) and a corrupt settings file degrades gracefully to an env-built agent.
- `-f <file>` — seed the task from a file (ola uses this over `-t` to dodge
  argv length limits on long prompts).
- Set `OPENHANDS_SUPPRESS_BANNER=1` to keep the startup banner out of stdout.

### Per-task isolation (the env vars that matter)

| Env var | Default | Use |
|---------|---------|-----|
| `OPENHANDS_PERSISTENCE_DIR` | `~/.openhands` | Config + conversations root. ola points this at the **per-task state dir** — the `cc`/`cx` equivalent of `CLAUDE_CONFIG_DIR`/`CODEX_HOME`. |
| `OPENHANDS_CONVERSATIONS_DIR` | `<persist>/conversations` | Where `base_state.json` lands. |
| `OPENHANDS_WORK_DIR` | `cwd` | Working directory the agent edits. |

## Config: `agent_settings.json`

The CLI loads `<persistence_dir>/agent_settings.json` via
`Agent.model_validate_json()` — it is a **serialized OpenHands SDK `Agent`**.
ola writes a fresh one per task so the full LLM knob surface survives (the
`--override-with-envs` path alone only carries model/key/url).

A minimal-but-complete document validates against the installed schema; tools
and agent-context are injected by the CLI's *runtime* config, so omit them. A
condenser is **not** added at runtime unless one is already present, so include
it for long-horizon runs:

```json
{
  "kind": "Agent",
  "llm": {
    "model": "...", "api_key": "<plaintext>", "base_url": "...",
    "usage_id": "agent", "stream": false, "drop_params": true,
    "temperature": 0.0, "max_output_tokens": 4096
  },
  "condenser": {
    "kind": "LLMSummarizingCondenser",
    "llm": { "...same llm...": "...", "usage_id": "condenser" }
  }
}
```

Notes:
- The `api_key` is written **plaintext** (the per-task state dir is private).
  `Agent.model_dump_json()` would mask it as `**********`, so you cannot
  round-trip a saved file to recover the key — hand-populate it. This mirrors
  what `_ola_inject_oh_settings` in `ola.sh` does for the sandbox host copy.
- ola sets only the LLM fields it configures (from `LLM_*` env) and lets the
  SDK default the rest. Re-validate the template on every SDK bump — the schema
  can drift.

## Output: the `--JSON Event-` stream

`--json` does **not** emit clean JSONL. `openhands_cli.utils.json_callback`
prints a marker line then a **pretty-printed, multi-line** `event.model_dump()`,
repeated per event:

```
--JSON Event--
{
  "kind": "MessageEvent",
  "source": "agent",
  "llm_message": { "content": [ { "type": "text", "text": "..." } ] }
}
```

Parsing rules (see `OpenHandsAgent._iter_events`):
- Split on the `--JSON Event--` marker; accumulate the multi-line block between
  markers.
- Parse each block with `json.JSONDecoder().raw_decode()`, **not**
  `json.loads()`. The CLI interleaves Rich console output (the "CONVERSATION
  SUMMARY" box, `Goodbye! 👋`, `Conversation ID:`) *after* the final event's
  JSON, in the same block — `raw_decode` parses the leading object and ignores
  the trailing text; `json.loads` chokes and silently drops the last event
  (often the error or final message).

Events of interest (`kind` is the discriminator):
- `MessageEvent` with `source == "agent"` → progress text and the final
  response (text lives at `llm_message.content[].text`).
- `ActionEvent` → `[<tool_name>] <summary>` progress lines.
- `ConversationErrorEvent` → **fatal**: the run loop hit an ERROR state (dead
  endpoint, auth, rate limit). Fields are `code` + `detail` (not
  `error`/`message`). An `AgentErrorEvent` is a *recoverable* tool observation —
  do not treat it as fatal.

### Completion & success

`--headless` exits **0 even when the conversation errored**, so exit code alone
is not a success signal. Use: `success = returncode == 0 and no
ConversationErrorEvent seen`. (And remember checkbox-is-truth governs actual
task completion in ola — this `success` is a soft signal for metrics.)

## Metrics: read `base_state.json` post-hoc

Headless `--json` carries no token-level chunks, so **TTFT and
decode-isolated tok/sec are unavailable** (set `ttft_ms=0`, `streamed=False` —
same honest limitation as the `ct` backend). Token economics are recovered
after exit from `<persistence_dir>/conversations/<id>/base_state.json`, which
persists the **full** (non-snapshot) `ConversationStats`:

```
stats.usage_to_metrics[<usage_id>] = {
  "model_name": "...",
  "accumulated_token_usage": {prompt_tokens, completion_tokens,
                              cache_read_tokens, cache_write_tokens, ...},
  "response_latencies": [{"latency": <sec>, "response_id": "...", "model": "..."}],
  "token_usages": [{"prompt_tokens": ..., ...}]
}
```

Map → `IterationStats`: sum tokens across all usages; `num_turns` = count of
`response_latencies`; `llm_ms` = sum of their `latency` (real per-call time);
`max_input_tokens` = max `token_usages[].prompt_tokens`; `models` from
`model_name` (fall back to the configured model). `decode_ms` reuses `llm_ms` as
a **conservative** throughput basis (it includes prefill/TTFT, so tok/sec is a
lower bound rather than a fabricated number).

## Why not the SDK (the in-process lock)

The OpenHands **SDK** `LLM` class guards every completion with a class-level
`_litellm_modify_params_lock` (a `ClassVar` `RLock`) held across the **entire
network round-trip**. Because it is shared by every `LLM` instance in the
process, threads (multiple agents in one process) all queue on it → **at most
one in-flight LLM request per process**, no matter how many agents you run
(measured flat on a 2→80 staircase against a self-hosted vLLM; `py-spy` shows
all workers parked at `_litellm_modify_params_ctx`). It is correctness
machinery (`litellm.modify_params` is process-global mutable state), not a
throughput knob. The subprocess-per-task model here sidesteps it entirely —
which is the whole reason this backend exists.
