"""Iteration statistics model."""

from pydantic import BaseModel


class IterationStats(BaseModel):
    """Token usage and timing for a single agent invocation."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    models: list[str] = []
