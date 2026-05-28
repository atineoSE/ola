"""Ola v2 event envelope (schema_version ``"2"``).

The envelope is the on-the-wire shape every emitted event takes, whether it
lands in ``<folder>/.ola/events.jsonl`` or is POSTed to a collector. The
authoritative field-by-field spec lives in :doc:`SCHEMA.md` (next to this
module); this dataclass is its executable mirror.

Lifecycle: ``started → working* → complete | failed``. There is no
``baseline``/``verified`` state — Ola has no built-in verifier. The ``data``
slot is opaque to the transport and collector: tasks that want to publish
metrics attach them there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "2"

# Allowed values for the ``status`` field. ``working`` may repeat; the other
# three are terminal/initial markers in the lifecycle.
VALID_STATUSES: frozenset[str] = frozenset({"started", "working", "complete", "failed"})


@dataclass(frozen=True)
class Event:
    """One immutable event envelope.

    All fields except ``data`` are scalar metadata that the collector indexes
    on. ``data`` is status-specific and treated as opaque downstream. Instances
    are frozen because an event, once assembled by the emitter, is a record of
    something that happened — sinks must not mutate it.
    """

    schema_version: str
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
            "schema_version": self.schema_version,
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
