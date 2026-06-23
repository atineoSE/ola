---
name: ola-design
description: Design philosophy and folder contract for the ola harness. Load whenever changing ola itself — every change must be checked against this philosophy.
version: 1.3.0
# 1.3.0: add the merge-back-is-reconciliation principle — 3-way merge,
#         identical-add no-op, conflict → intra-run retry → janitor escalation,
#         no smart-merge in the hot path (minor: new compatible guidance, defers
#         to CONTRACT.md for the load-bearing detail).
# 1.2.0: add the project-repo/agent-folder separation, Ralph-minimal PLAN.md +
#         JANITOR-NOTES.md provenance, and explicit per-task tick-surface
#         principles to the philosophy and the change-checklist (minor: new
#         compatible guidance, no existing rule changed).
---

# The ola design philosophy

ola exists for **extreme autonomy and extreme parallelism, orchestrated
entirely through files**. The agent folder is the database: numbered folders
run in lexicographic order, the tasks inside one PLAN.md are independent and
parallel-safe, every task runs with a fresh context in its own worktree, and
a ticked checkbox is the only completion signal the harness accepts. When a
task cannot proceed, the harness unblocks it automatically (the janitor)
before ever waiting on a human.

**Two repos, one owner.** ola runs from inside the **project repo** — its
working directory is the project being worked on. The **agent folder** is the
project's ola-owned sibling and holds the plan database; the agent never touches
it. Per-task worktrees spawn from the *project* repo, so a task agent's working
directory is a project worktree of code only — it never sees the agent folder,
and everything it needs arrives in its prompt or is recoverable from the
project's current state. ola gives each task its **tick surface as an explicit
per-task path** — a per-task PLAN.md copy injected into the worktree — and
reconciles the tick back onto the agent-folder PLAN.md itself, so the agent
checks off a file it was handed rather than one it has to discover.

**Merging code back is reconciliation, not a hard gate.** A worktree branched
off an older project HEAD; folding it back is a 3-way merge (object-store
`git merge-tree`), so non-overlapping edits auto-resolve and a collision with an
already-present path — even an untracked, byte-identical one like a shared empty
`__init__.py` — reconciles instead of aborting the apply. A merge that still
conflicts is a non-stagnant *failed attempt*, retried within the run: the retry
re-branches off the updated HEAD and usually merges cleanly, so collisions
self-heal without new state. Only a conflict that outlasts `--max-attempts` is
durable coupling, and it escalates through the ordinary janitor/blockers path —
never a model-driven smart-merge in the hot path. (See CONTRACT.md.)

**PLAN.md stays Ralph-minimal.** A task is its description plus its genuine
dependencies and policies — never ola internals or self-referential priming.
When the janitor relocates a blocked task it writes that same minimal PLAN.md
(the task as an unchecked checkbox + real deps/policies) and parks the
why-blocked reason, verification, and provenance in a separate
`JANITOR-NOTES.md` sidecar — for human review only, never fed back to an agent.

## Canonical contract

The canonical contract text lives at `src/ola/agents/CONTRACT.md` — it is
also inlined into the janitor prompt at runtime, so it is load-bearing, not
just documentation. **Read that file before reasoning about folder, blocked,
janitor, leftovers, or blockers semantics.** This skill intentionally does
not duplicate it; keep it the single source of truth and update it (plus the
prompt templates that consume it) in the same change as any behavior change.

## Checklist when changing ola

Challenge every change against these questions; push back on the change if
the answer is wrong:

- Does it keep PLAN.md authoritative (checkbox-is-truth), or does it invent a
  second completion signal?
- Does it keep runtime state (`tasks.json` status/attempts, blocked markers,
  worktrees) **intra-run only**, so a fresh `ola` invocation re-derives every
  task from PLAN.md? A prior run's `failed`/`blocked` verdict or attempt count
  must never gate the next run — the dev re-runs ola repeatedly, fixing things
  between runs. Prior results persist solely as monitor history (events.jsonl /
  STATS.jsonl) on a read-only path that never feeds execution.
- Does it preserve task independence, or does it introduce coupling between
  tasks inside one plan? Dependent work belongs in a later folder.
- Does it keep orchestration file-driven and auditable, or does it move state
  into memory, flags, or hidden services?
- Does it require a human where a janitor (or a new escape hatch) could act?
  Prefer aggressive automatic unblocking; escalate via `…-blockers/BLOCKERS.md`
  only as a last resort.
- Does it preserve folder-ordering semantics, including letter-suffixed
  siblings (`01-init` → `01a-init-leftovers` → `01b-init-blockers` → `02-…`)
  and mid-run folder discovery?
- Does it keep worktrees spawning from the **project** repo, with the agent's
  cwd a project worktree and the agent folder ola-only — or does it leak
  agent-folder paths/internals into what the task can see?
- Does it keep PLAN.md **Ralph-minimal** (task + real dependencies/policies, no
  harness internals), routing any blocked reason and provenance to a separate
  `JANITOR-NOTES.md` sidecar — or does it push ola internals or why-blocked
  narration into PLAN.md itself?
- Does it hand the agent its **tick surface as an explicit per-task path** ola
  injects into the prompt, or does it make the agent discover which file to
  check off?
- Does it stay agent-backend-agnostic (Claude Code, OpenHands, Codex), or
  does it branch on a specific backend?
- Does it scale with parallelism (locks around shared PLAN.md/git state,
  live-read concurrency cap), or does it silently serialize work?
- Does it keep merging code back a **reconciliation** (3-way merge, identical-add
  no-op, conflict → intra-run retry → janitor escalation), or does it make a
  recoverable collision a hard failure — or worse, smart-merge with a model in
  the hot path?
