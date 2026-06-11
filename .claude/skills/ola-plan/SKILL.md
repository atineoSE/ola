---
name: ola-plan
description: Turn a settled plan into an ola agent-folder tree — numbered sequential folders, with parallel-safe tasks inside each PLAN.md. Use at the end of a planning session, when the plan is agreed and the user says "create the ola plan for this", "make an ola plan out of this", or "lay this out for ola".
version: 1.0.0
---

# Create an ola plan

This skill is the bridge between **planning** and **running**. You (the agent)
have just gone back and forth with the user in an ordinary planning session and
settled on a plan for a large, long-horizon piece of work. ola does not invent
its own plan — your job here is to translate the plan you already agreed on into
the file-and-folder layout the ola harness executes.

The whole value of this step is the **decomposition**: deciding what must run in
**sequence** (separate numbered folders, run one after another) versus what is
**independent** and may run in **parallel** (tasks inside one folder's
`PLAN.md`). Get that split right and the run is fast and correct; get it wrong
and either work serializes needlessly or a task races ahead of a prerequisite
that has not happened yet.

## The contract you are encoding

The folder tree is the database the harness reads. These rules are
load-bearing — every plan you emit must satisfy them. (The canonical text lives
in the ola repo at `src/ola/agents/CONTRACT.md`; the essentials are inlined here
so the skill is self-contained.)

- **Numbered folders run in order.** Subfolders named `NN-description/`
  (`01-init/`, `02-api/`, `03-ui/`, …) are processed strictly in lexicographic
  order, one folder fully completing before the next starts. **Ordering between
  dependent stages comes only from folders.**
- **Tasks inside one `PLAN.md` are independent and parallel-safe.** Every
  `- [ ]` checkbox is one task that runs in isolation, in its own git worktree,
  in any order, possibly concurrently. There is **no ordering guarantee between
  tasks in the same plan — even at concurrency 1.** Never write a task that
  depends on a sibling task in the same folder having run first.
- **Fresh context per task.** Each task runs with a minimal prompt and no
  conversation history. Whatever context the task needs, it must be able to
  recover from the current state of the code — so each task description must be
  self-contained and point at the files/interfaces it touches.
- **Checkbox is truth.** A task is complete only when its `- [ ]` becomes
  `- [x]`. Plans are markdown todo lists, nothing more.

The single most important consequence: **if work item B depends on work item A,
A and B must live in different numbered folders** (A's folder earlier). If A and
B are independent, they should be two tasks in the *same* `PLAN.md` so they run
in parallel.

## Procedure

### 1. Recover the agreed plan and confirm scope

Summarize, in a few lines, the plan you and the user settled on — the goal and
the major pieces of work. Confirm you have the right plan before writing
anything. Do not silently expand scope or invent steps that were not discussed.

### 2. Find where the agent folder goes

ola runs against a **workspace** that holds two siblings: the **project repo**
(the source code) and the **agent folder** (the plan database, conventionally
named `agent/`). Determine the agent-folder path:

- If the user names a location, use it.
- Otherwise propose the conventional sibling layout and confirm:

  ```
  workspace/
    <project>/      # the source repo being worked on
    agent/          # ← the ola plan you are about to write
  ```

Create the agent folder if it does not exist. Do **not** create plan folders
inside the project's own source tree.

### 3. Decompose into stages (the sequential axis)

Break the plan into **dependency stages**. A new stage is required whenever a
piece of work *cannot start until* an earlier piece is finished — a shared
interface must exist first, a migration must land before code uses it, a library
must be built before its consumers, tests need the thing they test to exist.

Each stage becomes one numbered folder: `01-<slug>/`, `02-<slug>/`, … Order them
so that everything a stage needs is produced by an *earlier-numbered* stage. Use
short, descriptive slugs (`01-schema`, `02-api`, `03-frontend`).

Prefer **fewer, well-justified stages**. Every extra stage boundary is a barrier
that serializes the run, so only introduce one for a *real* dependency, not for
conceptual tidiness. Conversely, never merge two genuinely dependent pieces into
one stage to "save a folder."

### 4. Split each stage into parallel tasks (the parallel axis)

Within a stage, list the units of work that are **mutually independent** — they
touch different files/modules and neither needs the other's output. Each becomes
one `- [ ]` task in that folder's `PLAN.md`.

Good task granularity:

- **One self-contained, independently verifiable unit** — ideally one that ships
  with its own automated tests, since a task verifies itself before ticking.
- **Names the files/interfaces it owns**, so a fresh-context agent knows exactly
  where to work without the planning conversation.
- **Does not touch the same files as a sibling task** (parallel tasks merge back
  from separate worktrees; overlapping edits collide).

If you find two "tasks" in the same stage where one needs the other first, that
is a signal they belong in **different stages** — promote the dependent one to a
later folder.

### 5. Write the files

For each stage folder, write a `PLAN.md`:

```markdown
# Plan: <stage title>

<1–3 sentences: what this stage delivers and why its tasks are independent.>

- [ ] <task 1 — self-contained, names its files/interfaces>
- [ ] <task 2 — independent of task 1>
- [ ] <task 3 …>
```

Optional, per folder, when it helps:

- **`TASK-PROMPT.md`** — a per-task prompt template with `{{task_text}}` and
  `{{task_id}}` placeholders, used to drive each task reliably (e.g. project
  conventions, "run the test suite before ticking", how to signal blocked). If
  omitted, ola uses a sensible default. Recommended for non-trivial projects.
- **`.ola/concurrency`** — a single integer: how many tasks in this folder run
  at once (default 1). Set it (e.g. `4`) when a stage has many independent tasks
  worth running in parallel. The cap is re-read live during the run.

Do **not** write any `PLAN.md` for work that genuinely needs a human decision
that was never resolved in planning — leave it out and tell the user, rather
than encoding a guess as a task.

### 6. Show the user the tree and the reasoning

End by printing the folder tree you created and a one-line justification for each
stage boundary — *why* `02` must come after `01`. This is the user's chance to
catch a missed dependency or an over-eager serialization before they run `ola`.

## Self-check before you finish

Challenge the plan you wrote against each of these; fix it if the answer is wrong:

- Does **every** task sit in a `PLAN.md` whose sibling tasks are all independent
  of it? (No "do X, then in the next bullet do Y that needs X.")
- Is every cross-task dependency expressed as a **folder ordering**, never as
  task order within a plan?
- Could each task be handed to an agent with **no memory of this conversation**
  and still be done from the code alone?
- Are stage boundaries justified by **real** prerequisites, not tidiness — and
  is anything that could be parallel needlessly split across folders?
- Does each folder have a `PLAN.md` (the only thing that makes a folder run), and
  no leftover `SEED-PROMPT.md` or other generator file? ola executes authored
  plans only.

## Example shape

A plan to "add a tags feature to the notes app" might decompose to:

```
agent/
  01-schema/
    PLAN.md            # - [ ] add tags table + migration
                       # - [ ] add Tag model with tests
  02-api/              # needs the schema → later folder
    PLAN.md            # - [ ] POST /tags endpoint
                       # - [ ] GET /notes?tag= filter
                       # - [ ] attach/detach tag endpoints
    .ola/concurrency   # 3  (these three endpoints are independent)
  03-ui/               # needs the API → later folder
    PLAN.md            # - [ ] tag chip component
                       # - [ ] tag filter bar
    TASK-PROMPT.md     # project's component conventions + "run vitest before ticking"
```

`01` before `02` because the endpoints need the table; `02` before `03` because
the UI calls the endpoints. Within `02`, the three endpoints touch different
handlers and run in parallel.
