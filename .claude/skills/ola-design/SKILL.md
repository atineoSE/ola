---
name: ola-design
description: Design philosophy and folder contract for the ola harness. Load whenever changing ola itself — every change must be checked against this philosophy.
version: 1.0.0
---

# The ola design philosophy

ola exists for **extreme autonomy and extreme parallelism, orchestrated
entirely through files**. The agent folder is the database: numbered folders
run in lexicographic order, the tasks inside one PLAN.md are independent and
parallel-safe, every task runs with a fresh context in its own worktree, and
a ticked checkbox is the only completion signal the harness accepts. When a
task cannot proceed, the harness unblocks it automatically (the janitor)
before ever waiting on a human.

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
- Does it stay agent-backend-agnostic (Claude Code, OpenHands, Codex), or
  does it branch on a specific backend?
- Does it scale with parallelism (locks around shared PLAN.md/git state,
  live-read concurrency cap), or does it silently serialize work?
