"""Iteration statistics model."""

from pydantic import BaseModel


def cache_hit_rate(input_tokens: int, cache_read_tokens: int) -> float:
    """Cache hit rate as a percentage (0-100).

    ``input_tokens`` is the **total** prompt token count (already includes
    cache reads) as stored by both the Claude Code and OpenHands adapters.
    """
    if input_tokens == 0:
        return 0.0
    return cache_read_tokens / input_tokens * 100


class IterationStats(BaseModel):
    """Token usage and timing for a single agent invocation."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    models: list[str] = []
    tool_ms: int = 0
    llm_ms: int = 0
    max_input_tokens: int = 0
