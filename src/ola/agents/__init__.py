from ola.agents.base import Agent, AgentResponse
from ola.agents.claude_code import ClaudeCodeAgent
from ola.agents.claude_code_tui import ClaudeCodeTUIAgent
from ola.agents.codex import CodexAgent
from ola.agents.openhands import OpenHandsAgent

#: Every concrete backend, so harness code can reason about all of them
#: without branching on the one that happens to be configured.
AGENT_CLASSES = (
    ClaudeCodeAgent,
    ClaudeCodeTUIAgent,
    CodexAgent,
    OpenHandsAgent,
)

#: Per-task state directory names used by the backends (``.claude``,
#: ``.openhands``, ``.codex``). These hold live provider credentials and
#: session logs, so the agent folder must keep them out of git — every
#: backend's, not just the active one, since one folder may be re-run
#: with a different ``-a``.
STATE_DIR_NAMES = tuple(
    sorted({cls.state_dir_name for cls in AGENT_CLASSES if cls.state_dir_name})
)


def create_agent(name: str, model: str | None = None) -> Agent:
    """Factory to create an agent by name."""
    match name:
        case "claude-code" | "cc":
            return ClaudeCodeAgent(model=model)
        case "claude-tui" | "ct":
            return ClaudeCodeTUIAgent(model=model)
        case "openhands" | "oh":
            return OpenHandsAgent(model=model)
        case "codex" | "cx":
            return CodexAgent(model=model)
        case _:
            raise ValueError(
                f"Unknown agent: {name!r}. Use 'cc', 'ct', 'oh', or 'codex'."
            )


__all__ = [
    "AGENT_CLASSES",
    "STATE_DIR_NAMES",
    "Agent",
    "AgentResponse",
    "create_agent",
    "ClaudeCodeAgent",
    "ClaudeCodeTUIAgent",
    "CodexAgent",
    "OpenHandsAgent",
]
