from oloopa.agents.base import Agent, AgentResponse
from oloopa.agents.claude_code import ClaudeCodeAgent
from oloopa.agents.openhands import OpenHandsAgent


def create_agent(name: str, model: str | None = None) -> Agent:
    """Factory to create an agent by name."""
    match name:
        case "claude-code" | "cc":
            return ClaudeCodeAgent(model=model)
        case "openhands" | "oh":
            return OpenHandsAgent(model=model)
        case _:
            raise ValueError(f"Unknown agent: {name!r}. Use 'cc' or 'oh'.")


__all__ = [
    "Agent",
    "AgentResponse",
    "create_agent",
    "ClaudeCodeAgent",
    "OpenHandsAgent",
]
