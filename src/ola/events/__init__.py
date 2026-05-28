"""Ola v2 event schema and emission.

Public surface:

- :class:`~ola.events.schema.Event` — the v2 envelope dataclass + serializer.
- :class:`~ola.events.client.Emitter` — assembles envelopes and fans them out.
- :class:`~ola.events.client.Sink` / :class:`~ola.events.client.NullSink` —
  the sink interface and a no-op default.
- :class:`~ola.events.client.LocalSink` — appends events as JSON lines to
  ``<folder>/.ola/events.jsonl`` via a dedicated writer thread.
- :class:`~ola.events.client.HttpSink` — POSTs events to a collector on a
  background thread with a bounded drop-oldest queue.

The authoritative wire spec is ``src/ola/events/SCHEMA.md``.
"""

from ola.events.client import Emitter, HttpSink, LocalSink, NullSink, Sink
from ola.events.schema import SCHEMA_VERSION, VALID_STATUSES, Event

__all__ = [
    "Emitter",
    "Event",
    "HttpSink",
    "LocalSink",
    "NullSink",
    "SCHEMA_VERSION",
    "Sink",
    "VALID_STATUSES",
]
