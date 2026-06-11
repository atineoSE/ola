"""Ola event envelope.

The envelope is the shape every emitted event takes as one line in
``<folder>/.ola/events.jsonl``. The authoritative field-by-field spec lives in
:doc:`SCHEMA.md` (next to this module); this dataclass is its executable mirror.

Lifecycle: ``started → working* → complete | failed``. The ``data`` slot is a
status-specific payload typed in SCHEMA.md (``working`` carries a progress
``message``; ``working``/``complete``/``failed`` may carry a ``metrics`` block
with cumulative ``output_tokens``/``decode_ms``/``tokens_per_sec``). It is
stored verbatim; visualizing consumers interpret it and ignore unknown keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Allowed values for the ``status`` field. ``working`` may repeat; the other
# three are terminal/initial markers in the lifecycle.
VALID_STATUSES: frozenset[str] = frozenset({"started", "working", "complete", "failed"})


def metrics_block(*, output_tokens: int, decode_ms: int) -> dict[str, Any]:
    """Build the ``Metrics`` payload defined in SCHEMA.md.

    Counters are cumulative per attempt; ``tokens_per_sec`` is the lifetime
    average (``0`` when no decode time has been observed, e.g. backends that
    report token counts but not timing).
    """
    tokens_per_sec = (
        round(output_tokens / (decode_ms / 1000), 1) if decode_ms > 0 else 0
    )
    return {
        "output_tokens": output_tokens,
        "decode_ms": decode_ms,
        "tokens_per_sec": tokens_per_sec,
    }


@dataclass(frozen=True)
class Event:
    """One immutable event envelope.

    All fields except ``data`` are scalar metadata a consumer can index or
    group on. ``data`` is the status-specific payload defined in SCHEMA.md. Instances
    are frozen because an event, once assembled by the emitter, is a record of
    something that happened — sinks must not mutate it.
    """

    agent_id: str
    attempt: int
    seq: int
    ts: str
    folder: str
    task_id: str
    task_text: str
    agent_backend: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the envelope as a plain dict, ready for ``json.dumps``."""
        return {
            "agent_id": self.agent_id,
            "attempt": self.attempt,
            "seq": self.seq,
            "ts": self.ts,
            "folder": self.folder,
            "task_id": self.task_id,
            "task_text": self.task_text,
            "agent_backend": self.agent_backend,
            "status": self.status,
            "data": self.data,
        }

    def to_json(self) -> str:
        """Serialize to a single-line JSON string (one JSONL record)."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)
