# Ola event schema v2

**Authoritative.** This document is the source of truth for the Ola event
envelope. Sibling projects (collector, dashboard, fake-agent) consume it; when
they disagree with this file, this file wins. The executable mirror is
`schema.py` in this directory — keep the two in sync.

`schema_version` is `"2"`. v1 (the typed-task/verifier-delta schema frozen in
the SerenityCoding sibling) is superseded and is **not** accepted by v2
consumers.

## Envelope

Every event is a single JSON object with exactly these fields:

```json
{
  "schema_version": "2",
  "agent_id": "agent-0042",
  "attempt": 0,
  "seq": 3,
  "ts": "2026-05-27T14:03:11.482Z",
  "folder": "09-parallel-agents",
  "task_id": "t-abc1234",
  "task_text": "Refactor extractor to use shared HTTP client",
  "agent_backend": "cc",
  "status": "started",
  "data": {}
}
```

When written to `<folder>/.ola/events.jsonl` each event is one line. When sent
to a collector it is the JSON body of a `POST /events`.

## Fields

| Field | Type | Owner | Semantics |
| --- | --- | --- | --- |
| `schema_version` | string | emitter | Always `"2"` for this schema. Consumers reject other values. |
| `agent_id` | string | harness | Stable identifier for the agent process/worker that produced the event, e.g. `"agent-0042"`. Unique within a run; reused across an attempt's lifecycle. |
| `attempt` | integer | harness | Zero-based-or-more retry attempt for this `task_id`. Together with `agent_id` it scopes `seq`. |
| `seq` | integer | emitter | Monotonic counter **per `(agent_id, attempt)` pair**, starting at `0` for that pair's first event and incrementing by one per event. Lets a consumer order and gap-detect one attempt's stream regardless of arrival order. |
| `ts` | string | emitter | Event emission time, UTC, ISO-8601 with millisecond precision and a literal `Z`: `YYYY-MM-DDThh:mm:ss.sssZ`. |
| `folder` | string | harness | The agent subfolder name, e.g. `"09-parallel-agents"`. Not a path. |
| `task_id` | string | harness | The task's stable id from `enumerate_tasks` — `"t-" + sha1(text)[:8]` (with collision suffixes). |
| `task_text` | string | harness | Free-text task description (the PLAN.md checkbox text). **Not** a typed enum — v2 dropped the v1 task enum. |
| `agent_backend` | string | harness | The agent's mnemonic, e.g. `"cc"` (Claude Code), `"oh"` (OpenHands), `"cx"` (Codex). |
| `status` | string | emitter | One of `started`, `working`, `complete`, `failed`. See lifecycle. |
| `data` | object | task/agent | **Opaque** status-specific payload. The transport and collector never interpret it. Defaults to `{}`. |

## Lifecycle

```
started → working* → complete | failed
```

- `started` — emitted once when a worker begins an attempt.
- `working` — coarse-grained progress, zero or more times. Emitted from the
  agent's `on_progress` callback, coalesced to at most one per second per
  worker. Carries a short progress string under `data` (e.g.
  `{"message": "running tests"}`).
- `complete` — terminal; the attempt succeeded.
- `failed` — terminal; the attempt failed (agent error, stagnation, etc.).

There is **no** `baseline` or `verified` state: Ola has no built-in verifier.
A task that wants to publish metrics attaches them under `data` on a `working`
or `complete` event; consumers surface those generically rather than as typed
verifier deltas.

## Compatibility note

This schema is owned by Ola (beta posture): when a change is needed we break
the sibling collector/dashboard/fake-agent rather than contort Ola. Sibling
projects pin to this document, not the reverse.
