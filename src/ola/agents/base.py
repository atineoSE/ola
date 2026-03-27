from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ola.stats import IterationStats


@dataclass
class AgentResponse:
    """Response from an agent invocation."""

    output: str
    success: bool
    stats: IterationStats = field(default_factory=IterationStats)


class Agent(ABC):
    """Abstract base for coding agents."""

    state_dir_name: str = ""
    mnemonic: str = ""
    full_name: str = ""

    def __init__(self, model: str | None = None) -> None:
        self.model = model

    @abstractmethod
    def run(
        self,
        prompt: str,
        workdir: str,
        state_dir: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentResponse:
        """Send a prompt to the agent and return its response.

        Args:
            labels: Optional context passed from the outer loop, e.g.
                    ``{"folder": "01-solve", "phase": "loop-1"}``.
                    Agents may use this for trace metadata.
        """
        ...

    def version(self) -> str:
        """Return the agent's version string. Override in subclasses."""
        return ""
