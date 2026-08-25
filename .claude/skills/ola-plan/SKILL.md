---
name: ola-plan
description: Turn a settled plan into an ola agent-folder tree — numbered sequential folders, with parallel-safe tasks inside each PLAN.md. Use at the end of a planning session, when the plan is agreed and the user says "create the ola plan for this", "make an ola plan out of this", or "lay this out for ola".
version: 2.0.0
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
- **A worktree contains only what is committed at `HEAD`.** Each task gets
  `git worktree add -b <branch> <path> HEAD`, so the task sees the project
  exactly as the last commit left it. Three kinds of file are therefore
  **invisible to every task**, and only the first is the one people think of:
  a **gitignored** file; an **untracked** file (never `git add`ed — the common
  case, because it is what a file *you just wrote* is); and a **staged or
  modified but uncommitted** file, whose new content is not in `HEAD` yet.
  Anything an earlier task produced but did not commit is invisible the same
  way. No task can fix this from inside a plan; it has to be true of the repo
  before the run.
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
named `agent/`). ola is invoked **from inside the project repo** — its process
cwd is the project — and is pointed at the agent folder via the `--agent-folder`
argument (default `../agent`, i.e. the sibling). The agent folder is ola-owned;
the project repo is where the actual work lands. Determine the agent-folder path:

- If the user names a location, use it (it becomes ola's `--agent-folder`).
- Otherwise propose the conventional sibling layout and confirm:

  ```
  workspace/
    <project>/      # the source repo being worked on; ola runs from here (cwd)
    agent/          # ← the ola plan you are about to write (--agent-folder, default ../agent)
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

- **`TASK-PROMPT.md`** — **the only channel that carries anything from the
  agent folder into a task.** A task's working directory is a worktree of the
  *project* repo; ola never mounts, copies or names the agent folder there, so
  a task cannot open the agent folder's own files. Whatever a fresh-context
  agent must know that is not already in the project's code — the settled
  design, the conventions, the decisions the planning session reached — has to
  be **inlined into this template as prose**. If omitted, ola uses a sensible
  default. Recommended for non-trivial projects. A folder-local file **fully
  replaces** the default — ola does not merge them — so it must itself carry
  every placeholder it needs:
  - `{{task_text}}`, `{{task_id}}` — the task and its id.
  - `{{plan_path}}` — the per-task PLAN.md path the agent must tick. This is
    load-bearing: a ticked checkbox is the **only** completion signal ola reads,
    and the agent works in a project worktree that does not contain PLAN.md, so
    an override that omits `{{plan_path}}` leaves the agent unable to find the
    file to check off. Every task then reads back as *stagnant* (work done, never
    ticked) and the folder stalls on the consecutive-stagnant breaker. **Always
    include `{{plan_path}}` and the tick instruction in a custom override.**
  - `{{blocked_cmd}}` — the command a task runs to self-report BLOCKED; include
    it so the janitor escape hatch still works.

  **Inline the design; never point at it.** The tempting opening line is
  *"read `docs/design/<thing>.md` before you start"* — and it is the single
  most common way a plan fails. That document is the write-up of the planning
  session you have just finished, so it is minutes old and untracked; it is
  not gitignored, so it looks fine, but it is not in `HEAD` and therefore not
  in any worktree. Every task then begins by failing to find its own authority
  and either guesses or blocks. The reflex fix — commit the design doc to the
  project repo — is the **wrong** one: it puts an artifact of *how the work was
  scheduled* into the codebase permanently, to be read once by a robot. The
  design belongs in the agent folder, and it reaches the task by being **part
  of the prompt**.

  **Slice it per stage.** You already decomposed the work into folders, and
  there is one `TASK-PROMPT.md` per folder — so each stage's template carries
  the part of the design *that stage implements*, not the whole document. A
  fresh-context agent handed 25KB covering seven stages has to work out which
  three of them apply to it; handed its own slice, it just builds. Keep the
  full document in the agent folder (`design-notes.md`) as the human's copy
  and the source you slice from — it is never read by an agent.

  The rare exception is a path the **running code** opens — a fixture, an
  export, a schema loaded at runtime. That is data, not instructions, and it
  must be in `HEAD`; see step 5b.
- **`.ola/concurrency`** — a single integer: how many tasks in this folder run
  at once (default 1). Set it (e.g. `4`) when a stage has many independent tasks
  worth running in parallel. The cap is re-read live during the run.

At the **agent-folder root** (not per stage), one more file may be needed:

- **`allowlist.txt`** — the project's network egress. ola runs tasks inside a
  deny-by-default sandbox, so any host a task must reach and that the default
  policy does not already cover has to be listed here, one host per line
  (subdomains included; `#` comments allowed). **Always ask yourself what the
  plan reaches for**, because a missing host does not fail loudly — the task
  stalls or silently degrades, and the checkbox never gets ticked.

  The default `balanced` policy already covers AI provider APIs, package
  managers, code hosts and registries, so the common cases (installing
  dependencies, calling Anthropic/OpenAI) need **nothing**. Loopback traffic
  between processes inside the sandbox is not governed by policy either. Write
  the file only for genuinely project-specific hosts — a vendor API the project
  integrates with, documentation the plan fetches, a private registry — and put
  a one-line comment next to each saying which stage needs it.

  Network is not the only sandbox prerequisite worth stating. If the plan's
  tasks depend on credentials or binaries the sandbox provides (the injected
  Claude subscription credentials, an installed CLI), say so in the plan or in
  the spec the tasks read, so a fresh-context agent fails with a clear message
  instead of an inscrutable one.

  **UI-verification tasks specifically**: the sandbox is a Linux container, so
  any task that must visually verify a change (screenshot, render a page,
  check a UI diff) cannot use a macOS browser path like
  `/Applications/Google Chrome.app` — that instruction, if it lives in the
  project's own `CLAUDE.md`, silently doesn't apply in-sandbox. The sandbox
  image ships a real headless browser for this: `chromium-headless
  --screenshot=out.png --window-size=W,H <url>` (Playwright's self-contained
  Chromium, not an apt package). If a task needs it, say so explicitly in the
  task text or `TASK-PROMPT.md` — a fresh-context agent has no way to discover
  it otherwise and may conclude "no browser available" instead. Screenshotting
  a `localhost` dev server needs no `allowlist.txt` entry; a remote URL does.

- **`provision.sh`** — tooling the sandbox image does **not** ship. If the plan's
  tasks need a binary, service or runtime that is not already there (a database
  server, a language toolchain, a vendor CLI), the project installs it here
  rather than every unrelated sandbox carrying the weight. `ola-sandbox` /
  `ola-monitor` run it inside the sandbox on every create **and** reconnect, as
  the `agent` user with passwordless sudo, before any task starts; a non-zero
  exit refuses to start the sandbox. It must be **idempotent** — ola keeps no
  "already provisioned" marker, deliberately, so a broken internal fast-path
  guard shows up as a slow reconnect instead of hiding forever. Anything it
  downloads needs its host in `allowlist.txt` too. Worked example:
  `examples/provision.sh`.

- **`run-init.sh`** — what must be **true before the tasks start**. ola runs
  this one itself at startup: once per run, from the project repo, inside the
  sandbox only, and a non-zero exit aborts the run before the first task. Use it
  to reclaim state an earlier run left behind. Worked example:
  `examples/run-init.sh`.

  The two answer different questions — `provision.sh` is *what does this sandbox
  need installed* (once per sandbox), `run-init.sh` is *what must hold before
  dispatch* (once per run, including re-runs that never re-create the sandbox).

**Anything a task starts outlives the task.** If a task launches a long-lived
process — a database, a dev server, a queue worker, a file watcher — plan for
the fact that **nothing in the harness will stop it**. A daemonized process
reparents to PID 1: killing the task agent, ola itself, or the `sbx exec` that
launched the run does not touch it, and neither does deleting the worktree it
was started from — it keeps running on unlinked inodes, holding its memory and
its disk. Three consequences for the plan:

- **Address per-task state by path, not by a shared port or name.** Tasks in one
  `PLAN.md` run concurrently, so a fixed TCP port, a fixed database name or a
  fixed lockfile turns "independent tasks" into a race that shows up as a
  flaky, order-dependent failure. Prefer something per-worktree — a unix socket
  or a per-task directory. That is what actually makes such a task parallel-safe.
- **Tell the task to stop what it started**, in the task text or
  `TASK-PROMPT.md`; a shell `trap` is the usual form. This covers the happy path
  only — a timeout, a crash or a killed run skips it entirely.
- **Sweep the rest in `run-init.sh`.** That reclaims at run *boundaries*, so a
  leak inside one long run survives until the next run starts; leave the sandbox
  headroom for it.

Keep per-task state **under the worktree** (ola git-excludes `.ola/`, so
`<worktree>/.ola/<thing>` is invisible to `git add -A` and dies with the
worktree). It then cleans itself up in the normal case and is trivially
identifiable in the sweep.

**Input data is the prerequisite most often missed.** If the plan reads a file
the user supplied — an export, a fixture, a dump — walk the path from that file
to the task that opens it, and make sure it is *committed* before the run starts.
Vendoring it into the repo is not enough: dropping the file in place leaves it
untracked, and a `.gitignore` rule for the directory it lands in silently un-does
even a deliberate `git add`. The same applies between stages: if stage 02 writes
a dataset that stage 03 reads, that dataset must be **committed by stage 02's
task**, or it dies with the worktree that made it.

When the data is sensitive and ignoring it was the point, resolve the conflict
*with the user* before writing the plan — commit it to a private repo, or accept
that the run cannot see it — rather than writing a task that will block. Do not
plan a workaround that copies the file between worktrees at runtime; it makes the
run depend on state no worktree owns.

Do **not** write any `PLAN.md` for work that genuinely needs a human decision
that was never resolved in planning — leave it out and tell the user, rather
than encoding a guess as a task.

### 5b. Make every prerequisite reachable

List everything a task needs that is not already in the project's committed
code, and sort each item into one of exactly two kinds. The test is a single
question: **does the running code open this path, or does only the agent read
it?**

- **Only the agent reads it** — the design, conventions, decisions, background.
  This is *instructions*, and it must not be a path at all. Inline it into that
  stage's `TASK-PROMPT.md`, sliced to what the stage implements. Nothing is
  added to the project repo. This is the common case, and the default.
- **The code opens it** — a fixture a test loads, an export a script parses, a
  schema the app reads at runtime. This is *data*: it is addressed by path at
  runtime, so it has to be in the project's `HEAD` like any other source file.

For every path in the second kind, verify rather than assume:

```sh
cd <project-repo>
git cat-file -e HEAD:tests/fixtures/<thing>.json && echo present || echo MISSING
```

`git cat-file -e HEAD:<path>` is the one command that answers the actual
question, because a worktree is carved from `HEAD`. It catches all three ways a
file goes missing — ignored, untracked, and committed-but-since-modified — with
no second check to keep in sync. `git check-ignore` is **not** a substitute: it
answers only "is this ignored", and stays silent for an untracked file, which
is the case that actually bites.

Anything reported `MISSING` gets committed before the run. Two situations need
the user rather than a commit: a file ignored **on purpose** (secrets, a large
export) — resolve it with them, as below — and a project carrying unrelated
work in progress they may not want swept into a commit; show them the specific
paths and let them stage those.

Never plan around it. Copying a file between worktrees at runtime, or having
task 01 regenerate a document later tasks read, makes the run depend on state
no worktree owns.

**When in doubt, it is instructions.** Misfiling data as instructions fails
loudly and at once — the code cannot open a path that is not there. Misfiling
instructions as data is what leaves a design document sitting in a codebase
forever.

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
- Have you walked the plan for **every host a task reaches**, and either
  confirmed the default sandbox policy covers it or listed it in
  `allowlist.txt`? State the conclusion to the user either way — "nothing
  needed, the defaults cover it" is a real answer, silence is not.
- Does any task need to **visually verify a UI change**? If so, does it name
  `chromium-headless` (the sandbox's baked-in headless Chromium) rather than a
  macOS-only browser path, and does a remote URL it screenshots appear in
  `allowlist.txt`?
- Does any task start a **long-lived process**? If so: is it addressed by a
  per-task path rather than a shared port/name, is the task told to stop it, and
  does `run-init.sh` sweep what a crashed run would leave behind?
- Does any `TASK-PROMPT.md` **point at a document instead of carrying it**? A
  task cannot open the agent folder, and the design write-up is not in the
  project's `HEAD`, so a pointer reaches nothing — inline that stage's slice as
  prose. Grep your own templates for `read `, `see ` and `.md` before you
  finish.
- For the paths that remain — the ones **running code opens**, plus anything an
  earlier stage hands to a later one — is each present in the project's `HEAD`?
  Run `git cat-file -e HEAD:<path>` on each rather than assuming: a file
  sitting in the user's checkout tells you nothing about whether a worktree
  will have it, and `git check-ignore` will not tell you either — it is silent
  for the untracked file, which is the usual failure.

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
