from ola.agents.base import Agent, AgentResponse
from ola.agents.claude_code import ClaudeCodeAgent
from ola.agents.codex import CodexAgent
from ola.agents.openhands import OpenHandsAgent


def create_agent(name: str, model: str | None = None) -> Agent:
    """Factory to create an agent by name."""
    match name:
        case "claude-code" | "cc":
            return ClaudeCodeAgent(model=model)
        case "openhands" | "oh":
            return OpenHandsAgent(model=model)
        case "codex" | "cx":
            return CodexAgent(model=model)
        case _:
            raise ValueError(f"Unknown agent: {name!r}. Use 'cc', 'oh', or 'codex'.")


__all__ = [
    "Agent",
    "AgentResponse",
    "create_agent",
    "ClaudeCodeAgent",
    "CodexAgent",
    "OpenHandsAgent",
]
