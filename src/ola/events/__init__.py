"""Ola event schema and emission.

Public surface:

- :class:`~ola.events.schema.Event` — the envelope dataclass + serializer.
- :class:`~ola.events.client.Emitter` — assembles envelopes and fans them out.
- :class:`~ola.events.client.Sink` / :class:`~ola.events.client.NullSink` —
  the sink interface and a no-op default.
- :class:`~ola.events.client.LocalSink` — appends events as JSON lines to
  ``<folder>/.ola/events.jsonl`` via a dedicated writer thread.

The authoritative wire spec is ``src/ola/events/SCHEMA.md``.
"""

from ola.events.client import Emitter, LocalSink, NullSink, Sink
from ola.events.schema import VALID_STATUSES, Event, metrics_block

__all__ = [
    "Emitter",
    "Event",
    "LocalSink",
    "NullSink",
    "Sink",
    "VALID_STATUSES",
    "metrics_block",
]
