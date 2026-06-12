# The ola contract

ola is built for **extreme autonomy and extreme parallelism, orchestrated
entirely through files**. The harness never asks a human unless every
automatic escape hatch is exhausted, and it never serializes work that can
run in parallel.

## File-driven orchestration

The agent folder (a sibling of the project being worked on) is the database.
Everything the harness and its agents need to coordinate lives in plain files
and folders — auditable, diffable, and easy for both humans and agents to
inspect and edit.

- **Numbered folders run in order.** Subfolders named `NN-description/`
  (e.g. `01-init/`, `02-setup/`) are processed in lexicographic order, one
  folder at a time.
- **Tasks inside one PLAN.md are independent and parallel-safe.** Each
  `- [ ]` checkbox line in a folder's `PLAN.md` is one task that can be
  worked on in isolation, in any order, concurrently. Work that depends on
  other work belongs in a *later folder*, never in the same plan.
- **Fresh context per task.** Each task runs in its own git worktree with a
  minimal prompt. Whatever context an agent needs it recovers from the
  current state of the code, not from conversation history.
- **Checkbox is truth.** A ticked `- [x]` in PLAN.md is the only completion
  signal the harness accepts. Work that is not reflected in a tick did not
  happen, no matter what the agent claims.
- **A folder is finished only when nothing is left undone.** The harness keeps
  retrying a folder's tasks until every checkbox is ticked or its task has been
  relocated to a sibling leftovers/blockers folder (the janitor removes the
  relocated line). Each task is retried up to `--max-attempts` **within a run**;
  a task that exhausts its attempts stops being retried *for that run*. Only
  then, if PLAN.md still has unticked lines, does the harness **bail out and
  stop** — it never advances to the next folder leaving unfinished work behind.
- **Run state is intra-run; a new run starts fresh from PLAN.md.** The next
  `ola` invocation re-derives every task's status from the checkboxes, so an
  unticked task — whether it was failed, blocked, or merely crash-interrupted
  last time — starts over with a full attempt budget again (`--max-attempts` is
  a per-run budget). The developer re-runs ola repeatedly, fixing prompts, env,
  or code between runs; a past failure must never gate the next attempt. Prior
  runs persist only as monitor history (events.jsonl / STATS.jsonl), never as an
  execution gate. To park a task permanently, edit the plan — remove the line or
  relocate it to a blockers/leftovers folder.

## Blocked tasks

A task agent that cannot complete its task because something **out of
scope** is missing (a prerequisite, a credential, an undecided design) must
not guess and must not tick its checkbox. It signals BLOCKED by running the
`ola-blocked` script provisioned into its worktree with a one-sentence
reason, then stops. Blocked tasks are never retried as-is; the reason is
recorded and a janitor is dispatched.

If a task both ticks its checkbox and reports blocked, the tick wins —
checkbox is truth.

## The janitor

When a task reports BLOCKED, the harness immediately spawns a **janitor
agent** whose single mandate is to unblock aggressively so the run keeps
moving without a human. The janitor produces exactly one of two outcomes:

1. **Unblock (strongly preferred).** If the missing prerequisite can be
   produced by an agent, the janitor adds the prerequisite work as new
   unchecked checkboxes to the *current* folder's PLAN.md (so it is picked
   up in the same run), removes the blocked task's line, and moves the
   blocked task into a new sibling **leftovers folder** named
   `<NN><letter>-<base>-leftovers/` containing a PLAN.md with a short note
   (the task was blocked, why, and that prerequisites are assumed complete
   by the time the folder runs) followed by the task as an unchecked
   checkbox.
2. **Escalate (last resort).** If a human or an unobtainable resource is
   genuinely required, the janitor creates a sibling **blockers folder**
   named `<NN><letter>-<base>-blockers/` containing a `BLOCKERS.md` — *not*
   a PLAN.md — with the task text, the worker's reason, and the janitor's
   own explanation of why it could not unblock. Folders without a PLAN.md
   are skipped by the harness, so escalated work waits for a human while
   everything else keeps advancing.

## Folder suffix convention

Janitor-created folders take the next free lowercase letter suffix on the
parent folder's number: `01-init` → `01a-init-leftovers`, `01b-init-blockers`.
Letter-suffixed folders sort lexicographically *between* their parent and
the next number, so leftovers run right after the folder that spawned them
and before `02-…`. After `z`, suffixes extend as `za`, `zb`, … to preserve
ordering. A leftovers folder that blocks again allocates the next letter on
the same number (`01b-init-leftovers`), never a stacked
`…-leftovers-leftovers` name.
