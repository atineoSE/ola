from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from ola.stats import IterationStats


@dataclass
class AgentResponse:
    """Response from an agent invocation."""

    output: str
    success: bool
    stats: IterationStats = field(default_factory=IterationStats)


class ProgressCallback(Protocol):
    """Coarse-grained progress callback supplied by the harness.

    ``message`` is a short status string (e.g. tool name or message snippet).
    ``metrics`` is the optional ``Metrics`` block from ``ola/events/SCHEMA.md``
    — cumulative ``output_tokens``/``decode_ms``/``tokens_per_sec`` for the
    attempt so far (build it with :func:`ola.events.schema.metrics_block`).
    Backends that cannot report usage mid-stream just call with the message.
    """

    def __call__(self, message: str, metrics: dict[str, Any] | None = None) -> None: ...


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
        on_progress: ProgressCallback | None = None,
    ) -> AgentResponse:
        """Send a prompt to the agent and return its response.

        Args:
            labels: Optional context passed from the outer loop, e.g.
                    ``{"folder": "01-solve", "phase": "loop-1"}``.
                    Agents may use this for trace metadata.
            on_progress: Optional coarse-grained progress callback. If
                    provided, agents may invoke it with a short status
                    string (e.g. tool name or message snippet) at natural
                    boundaries, optionally with a cumulative throughput
                    ``metrics`` block. Implementations may treat it as a
                    no-op.
        """
        ...

    def version(self) -> str:
        """Return the agent's version string. Override in subclasses."""
        return ""
