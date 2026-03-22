from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AgentResponse:
    """Response from an agent invocation."""

    output: str
    success: bool


class Agent(ABC):
    """Abstract base for coding agents."""

    state_dir_name: str = ""

    def __init__(self, model: str | None = None) -> None:
        self.model = model

    @abstractmethod
    def run(
        self, prompt: str, workdir: str, state_dir: str | None = None
    ) -> AgentResponse:
        """Send a prompt to the agent and return its response."""
        ...
