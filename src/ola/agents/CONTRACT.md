# The ola contract

ola is built for **extreme autonomy and extreme parallelism, orchestrated
entirely through files**. The harness never asks a human unless every
automatic escape hatch is exhausted, and it never serializes work that can
run in parallel.

## Project repo and agent folder

ola is invoked from **inside the project repo** — the process working
directory is the project being worked on. The **agent folder** is that
project's sibling, passed as `--agent-folder` (default `../agent`), and is
owned entirely by ola: it holds the numbered folders, their PLAN.md files,
the per-task worktrees, and the monitor history. The project repo holds the
code under change; the agent folder is the orchestration database (below).

Per-task git worktrees spawn from the **project** repo, so a task agent's
working directory is a project worktree that contains only project code. The
agent never sees the agent folder and never needs to: whatever it must know
arrives in its prompt or is recoverable from the project's current state.
Finished code changes merge back onto the project repo, and ola reconciles each
task's checkbox tick onto the agent-folder PLAN.md on the agent's behalf.

## The agent commits its own verified work; harness commits are bookkeeping

The task agent **commits its changes in its worktree before ticking** — the
prompt requires it. That commit is where the project's own quality gate runs:
if the project has git hooks (a `pre-commit` running linters or type checks),
they fire on the agent's commit, *inside the agent's loop*, where the agent
sees the failure and fixes it before finishing. A closed feedback loop is only
possible here — after the agent ticks, its context is gone.

Every commit ola itself makes is therefore pure bookkeeping and runs with
`--no-verify`: the worktree fallback commit (a safety net for a backend that
ticked without committing), the reconciliation commit that lands the merged
tree on the project repo, and the checkbox-tick commit in the agent folder. A
project git hook is the *agent's* gate, exercised once in the agent's worktree;
it must never re-fire as a hidden post-hoc veto on ola's reconciliation commit
— which would run the hook over the *combined* tree (this task plus siblings)
and fail a task for coupling the independence contract already assumes away.
The agent's own commit message is preserved onto the project repo
(`git commit -C <sha>`); the synthetic `ola: <folder> <task>: …` message is only
the fallback when the agent left the worktree uncommitted.

## Merging code back is reconciliation, not a hard gate

A task's worktree branched off the project HEAD at *its* start; by the time it
finishes, sibling tasks may have landed. Folding its commit back is therefore a
**3-way merge** (`git merge-tree` in the object store), not a brittle apply: git
auto-resolves non-overlapping edits, and a collision with a path that already
exists in the project tree — even an untracked, byte-identical one such as a
shared empty `__init__.py` another task just added — reconciles instead of
aborting. An identical add is no diff against HEAD, so it simply drops out.

A merge that still conflicts is **not** a hard failure — it is the same
non-stagnant failed attempt as any other, and is **retried within the run**.
Because the retry re-branches off the *now-updated* project HEAD, it sees the
winner's already-landed files and usually merges cleanly; most collisions
self-heal this way with no new state (intra-run only — a fresh run still
re-derives from PLAN.md). Only a conflict that survives the whole
`--max-attempts` budget is treated as durable coupling (a plan-independence
violation): it is recorded as blocked and **escalated through the ordinary
janitor/blockers path** for a human, never smart-merged by a model in the hot
path.

## File-driven orchestration

The agent folder is the database. Everything the harness and its agents need
to coordinate lives in plain files and folders — auditable, diffable, and easy
for both humans and agents to inspect and edit.

- **Numbered folders run in order.** Subfolders named `NN-description/`
  (e.g. `01-init/`, `02-setup/`) are processed in lexicographic order, one
  folder at a time.
- **Tasks inside one PLAN.md are independent and parallel-safe.** Each
  `- [ ]` checkbox line in a folder's `PLAN.md` is one task that can be
  worked on in isolation, in any order, concurrently. Work that depends on
  other work belongs in a *later folder*, never in the same plan.
- **Fresh context per task.** Each task runs in its own git worktree of the
  project repo with a minimal prompt. Whatever context an agent needs it
  recovers from the current state of the code, not from conversation history.
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
   `<NN><letter>-<base>-leftovers/`. That folder's PLAN.md is minimal — the
   task as an unchecked checkbox plus only its genuine dependencies or
   policies — and says nothing about why it was blocked or that a janitor
   produced it; the next agent to pick it up reads it as an ordinary task.
   The blocked reason, the janitor's verification, and the task's provenance
   go into a separate `JANITOR-NOTES.md` sidecar in the same folder, written
   for human review only — the harness never feeds it to an agent.
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
