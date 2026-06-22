---
name: openhands-sdk
description: How to configure LLM and Agent classes with the OpenHands SDK
version: 1.2.0
---


# Agent SDK Configuration Guide

This guide covers how to configure the OpenHands SDK's `LLM` and `Agent` classes, including loading configuration from environment variables, using default tools, and streaming responses.

## Table of Contents

1. [Configuring the LLM Class](#configuring-the-llm-class)
2. [Loading LLM Config from Environment Variables](#loading-llm-config-from-environment-variables)
3. [Configuring the Agent Class](#configuring-the-agent-class)
4. [Using Default Tools](#using-default-tools)
5. [Streaming Responses](#streaming-responses)

---

## Configuring the LLM Class

The `LLM` class provides a unified interface for interacting with various language models through the [litellm](https://docs.litellm.ai/) library.

### Basic Configuration

```python
from openhands.sdk import LLM
from pydantic import SecretStr

# Minimal configuration - requires model name
llm = LLM(model="claude-sonnet-4-20250514")

# With API key
llm = LLM(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr("your-api-key")
)
```

### Key LLM Fields

| Field | Type | Default | Description |
|-------|------|--------|-------------|
| `model` | `str` | `"claude-sonnet-4-20250514"` | Model name (e.g., "gpt-4o", "claude-sonnet-4-20250514") |
| `api_key` | `str \| SecretStr \| None` | `None` | API key for authentication |
| `base_url` | `str \| None` | `None` | Custom API base URL |
| `api_version` | `str \| None` | `None` | API version (e.g., for Azure) |
| `num_retries` | `int` | `5` | Number of retry attempts |
| `timeout` | `int \| None` | `300` | HTTP timeout in seconds |
| `temperature` | `float \| None` | `None` | Sampling temperature (0.0-1.0) |
| `top_p` | `float \| None` | `None` | Nucleus sampling parameter |
| `max_output_tokens` | `int \| None` | `None` | Maximum output tokens |
| `stream` | `bool` | `False` | Enable streaming responses |
| `native_tool_calling` | `bool` | `True` | Use native tool calling |

### AWS Configuration

```python
llm = LLM(
    model="bedrock/anthropic.claude-3-sonnet-8-2025-02-20",
    aws_access_key_id=SecretStr("your-aws-key"),
    aws_secret_access_key=SecretStr("your-aws-secret"),
    aws_region_name="us-west-2"
)
```

### OpenRouter Configuration

```python
llm = LLM(
    model="openrouter/anthropic/claude-3.5-sonnet",
    api_key=SecretStr("your-openrouter-key")
)
```

---

## Loading LLM Config from Environment Variables

The SDK does not automatically load env vars into LLM configs, but you can easily do this using Python's `os.environ` or `os.getenv`:

```python
import os
from openhands.sdk import LLM
from pydantic import SecretStr

# Load from environment variables
def create_llm_from_env() -> LLM:
    """Create an LLM instance from environment variables."""
    model = os.getenv("OPENAI_MODEL", "claude-sonnet-4-20250514")
    api_key = os.getenv("OPENAI_API_KEY")
    
    # Optional configurations
    base_url = os.getenv("OPENAI_BASE_URL")
    api_version = os.getenv("OPENAI_API_VERSION")
    
    return LLM(
        model=model,
        api_key=SecretStr(api_key) if api_key else None,
        base_url=base_url,
        api_version=api_version,
    )
```

### Environment Variable Pattern

Create a `.env` file for your project:

```bash
# LLM Configuration
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_MODEL=claude-sonnet-4-20250514

# Optional
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_VERSION=2024-12-01-preview

# Azure specifics (if using Azure OpenAI)
AZURE_API_KEY=your-azure-key
AZURE_DEPLOYMENT_NAME=gpt-4
AZURE_BASE_URL=https://your-resource.openai.azure.com/
AZURE_API_VERSION=2024-12-01-preview
```

### Using python-dotenv

```python
from dotenv import load_dotenv
from openhands.sdk import LLM

load_dotenv()  # Load .env file

llm = LLM(
    model=os.getenv("OPENAI_MODEL", "claude-sonnet-4-20250514"),
    api_key=os.getenv("OPENAI_API_KEY"),
)
```

---

## Configuring the Agent Class

The `Agent` class wraps an LLM and provides agentic behavior with tool calling, conversation management, and more.

### Basic Agent Creation

```python
from openhands.sdk import Agent, LLM
from openhands.sdk.llm import Message, TextContent
from pydantic import SecretStr

# Create LLM
llm = LLM(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr("your-api-key")
)

# Create Agent with the LLM
agent = Agent(llm=llm)

# Run a conversation
conversation = agent.run("Hello, how are you?")
```

### Agent with Custom Tools

```python
from openhands.sdk import Agent, LLM, Tool

llm = LLM(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr("your-api-key")
)

# Define custom tools
my_tool = Tool(
    name="my_custom_tool",
    description="A custom tool that does something useful",
    # The callable that implements the tool
    callable=my_function,
)

# Create agent with tools
agent = Agent(
    llm=llm,
    tools=[my_tool],
)
```

### Agent Settings Schema

The `AgentSettings` class provides a structured way to configure agents:

```python
from openhands.sdk import Agent, AgentSettings, LLM
from pydantic import SecretStr

# Create settings
settings = AgentSettings(
    agent="CodeActAgent",
    llm=LLM(
        model="claude-sonnet-4-20250514",
        api_key=SecretStr("your-api-key")
    ),
    tools=[]  # Add tools as needed
)

# Create agent from settings
agent = Agent(**settings.model_dump(exclude={"tools"}), tools=settings.tools)
```

### Agent Fields

| Field | Type | Default | Description |
|-------|------|--------|-------------|
| `llm` | `LLM` | Required | The language model instance |
| `tools` | `list[Tool]` | `[]` | Tools available to the agent |
| `system_prompt` | `str \| None` | `None` | Custom system prompt |
| `system_prompt_kwargs` | `dict \| None` | `None` | System prompt template variables |
| `condenser` | `CondenserBase \| None` | `None` | Conversation condenser |

---

## Using Default Tools

The SDK provides a set of default tools that can be easily configured.

### Getting Default Tools

```python
from openhands.tools import get_default_tools

# Get default tools (includes browser, file_editor, terminal, task_tracker)
tools = get_default_tools(enable_browser=True)

# Get tools without browser (for CLI mode)
tools = get_default_tools(enable_browser=False)
```

### Default Tool List

The default tools include:

| Tool | Description |
|------|------------|
| `TerminalTool` | Execute terminal commands |
| `FileEditorTool` | file_editor and manage files |
| `TaskTrackerTool` | Track task progress |
| `BrowserToolSet` | Browser automation (optional) |

### Registering Default Tools

```python
from openhands.tools import register_default_tools

# Register default tools in the global registry
register_default_tools(enable_browser=True)

# Later, resolve tools by name
from openhands.sdk import resolve_tool

terminal = resolve_tool("terminal")
if terminal:
    print(f"Found tool: {terminal.name}")
```

### Using Default Agent

```python
from openhands.sdk import Agent, LLM
from openhands.tools import get_default_agent
from pydantic import SecretStr

# Create LLM
llm = LLM(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr("your-api-key")
)

# Get the default agent with all default tools
agent = get_default_agent(llm=llm)

# Or in CLI mode (without browser)
agent = get_default_agent(llm=llm, cli_mode=True)
```

---

## Streaming Responses

The SDK supports streaming responses from language models, allowing you to receive tokens as they are generated.

### Option 1: Enable Streaming at LLM Level

```python
from openhands.sdk import LLM
from pydantic import SecretStr

llm = LLM(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr("your-api-key"),
    stream=True  # Enable streaming globally
)
```

### Option 2: Enable Streaming Per-Request

```python
from openhands.sdk import LLM, Message, TextContent
from openhands.sdk.llm import LLMStreamChunk

def on_token_callback(chunk: LLMStreamChunk):
    """Process each streaming token chunk."""
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)

messages = [Message(role="user", content=[TextContent(text="Hello")])]

response = llm.completion(messages, stream=True, on_token=on_token_callback)
```

### Using with Agents

```python
from openhands.sdk import Agent, LLM
from pydantic import SecretStr

llm = LLM(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr("your-api-key"),
    stream=True
)

agent = Agent(llm=llm)

# Stream agent responses
for event in agent.run_stream("Write a short story"):
    if hasattr(event, 'content'):
        print(event.content, end="")
```

### Token Callback Type

```python
from openhands.sdk.llm import LLMStreamChunk, TokenCallbackType

# Type alias for callback functions
TokenCallbackType = Callable[[LLMStreamChunk], None]
```

---

## Complete Example

Here's a complete example that combines all the concepts:

```python
import os
from dotenv import load_dotenv
from openhands.sdk import Agent, LLM, Tool
from openhands.sdk.llm import Message, TextContent
from openhands.tools import get_default_tools
from pydantic import SecretStr

# Load environment variables
load_dotenv()

# Create LLM from environment
def create_llm() -> LLM:
    return LLM(
        model=os.getenv("OPENAI_MODEL", "claude-sonnet-4-20250514"),
        api_key=SecretStr(os.getenv("OPENAI_API_KEY", "")),
    )

# Get LLM with streaming enabled
llm = create_llm()
llm.stream = True

# Get default tools
tools = get_default_tools(enable_browser=False)

# Create agent
agent = Agent(
    llm=llm,
    tools=tools,
)

# Run with streaming
def handle_token(chunk):
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)

print("Running agent with streaming...")
for event in agent.run_stream("Say hello"):
    if hasattr(event, 'content'):
        print(event.content, end="")
print("\nDone!")
```

---

## API Reference

### LLM Class

```python
from openhands.sdk import LLM

llm = LLM(
    model="claude-sonnet-4-20250514",      # Model name
    api_key=SecretStr("key"),                 # API key
    base_url=None,                            # Custom base URL
    num_retries=5,                        # Retry count
    timeout=300,                          # HTTP timeout
    stream=False,                         # Enable streaming
)
```

### Agent Class

```python
from openhands.sdk import Agent

agent = Agent(
    llm=llm,                            # Required LLM
    tools=[],                            # List of tools
    system_prompt=None,                    # Custom system prompt
    condenser=None,                     # Conversation condenser
)
```

### Tool Class

```python
from openhands.sdk import Tool

tool = Tool(
    name="my_tool",                      # Tool name
    description="What this tool does",      # Tool description
    callable=my_function,                # Callable implementation
)
```

---

## Thread safety: warm up the import before fanning out

`import openhands.sdk` transitively does `import litellm`. That **first**
import is not safe to run from several threads at once: a concurrent first
import trips CPython's import-lock deadlock detector
(`deadlock detected by _ModuleLock('litellm…')`) and leaves `litellm`
half-initialised, so every later importer dies with
`partially initialized module 'litellm' … has no attribute
'litellm_core_utils' (most likely due to a circular import)`.

If you import the SDK lazily inside worker threads (as ola's `oh` backend
does, to keep `import ola` cheap for the other backends), force the import
once on the main thread **before** dispatching the pool:

```python
def warm_up() -> None:
    # Run once, serially, before any worker thread starts.
    import litellm            # noqa: F401
    import openhands.sdk      # noqa: F401  (pulls litellm in too)
    import openhands.tools    # noqa: F401
```

The same applies to any process-global setup the SDK path performs
(`os.environ` writes, `litellm.ssl_verify`, telemetry init such as Laminar):
do it once here rather than per worker. Telemetry that must initialise
*before* the SDK import (e.g. Laminar's HTTP exporter) goes at the top of the
warm-up, ahead of the `import openhands.sdk` line.

## In-process concurrency: the SDK serializes every LLM call

**The SDK does not run concurrent completions within one process.** `LLM`
guards every model call with a **class-level lock held across the entire network
round-trip** (verified in `openhands-sdk` v1.22.0):

```python
# openhands/sdk/llm/llm.py
class LLM(...):
    _litellm_modify_params_lock: ClassVar[threading.RLock] = threading.RLock()  # shared by ALL LLM instances

    @contextmanager
    def _litellm_modify_params_ctx(self, flag):
        with self._litellm_modify_params_lock:        # held for the whole call below
            old = litellm.modify_params
            litellm.modify_params = flag
            try:
                yield                                  # ← the actual completion / streaming happens here
            finally:
                litellm.modify_params = old
```

The transport path wraps each completion in this context manager, so the lock is
held for the full request. Because it is a `ClassVar`, it is shared across *every*
`LLM` instance in the process. **Threads (multiple agents in one process) all
queue on it → at most one in-flight LLM request regardless of how many agents
you run.** A subprocess-per-agent model is unaffected (each process has its own
`litellm` globals and its own lock).

Why the lock exists: `litellm.modify_params` is **process-global** mutable state
that the SDK toggles per call, so the lock prevents one thread changing the
global mid-flight of another. It is *correctness* machinery, not a throughput
knob.

How to confirm you've hit it: a `py-spy dump` shows every worker thread parked at
`_litellm_modify_params_ctx`; server-side concurrency (e.g. vLLM
`num_requests_running`) sits at ~1 while local CPU/RAM are idle and throughput is
flat as you raise the agent count. (Measured exactly this on a 2→80 staircase
against a self-hosted vLLM.)

Clean ways to get real in-process concurrency:
- **Run agents in separate processes** (the robust option) — no shared lock.
- **If, and only if, every `LLM` in the process uses the same `modify_params`
  value** (typical for a single uniform config), the per-call toggle is
  redundant: set `litellm.modify_params` once in `warm_up()` and neutralize the
  context manager (e.g. monkeypatch `LLM._litellm_modify_params_ctx` to a no-op).
  This is a **private-API monkeypatch** — guard it with `hasattr` so an SDK
  rename fails safe back to the serialized path, and re-verify on every SDK bump.
- **Upstream fix:** scope the lock to the brief global toggle only (not the
  `yield`/network call), or set the global once at `LLM` init. Prefer this.
