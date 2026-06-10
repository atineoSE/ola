# Dummy project — a runnable ola example

A minimal, **safe-to-run** example project that exercises the main ola features:
a seed phase, a plan run at the default cap (one task at a time), and a plan run
in parallel. The tasks are trivial (find the date, write small files) so you can
watch the harness work without a large bill or a real codebase.

> These folders double as the happy-path e2e scenarios: the hermetic,
> network-free suite in [`tests/e2e/`](../../tests/e2e/) copies them verbatim
> and drives the real pipeline over them with a stub agent. If you edit this
> example, run `make test-e2e` — the tests keep it from silently rotting, and
> this folder stays the human-facing version you can run with a real agent.

## Layout

```
dummy-project/             # workspace root
  dummy-project/           # the project repo — your "source code" (empty placeholder here)
  agent/
    .env.example           # copy to .env and fill in your provider
    01-find-date/          # SEED-PROMPT.md → agent generates PLAN.md, then runs it
    02-utils/              # a ready-made PLAN.md, run at the default cap of 1
    03-parallel/           # a PLAN.md + .ola/concurrency=2 → runs tasks in parallel
```

The layout illustrates ola's ordering contract: tasks **within** a `PLAN.md`
are implicitly independent and parallel-safe (the concurrency cap only decides
how many run at once), while ordering between dependent stages comes from the
indexed folders — `01-find-date` completes before `02-utils` starts, and so on.
Work that depends on other work belongs in a later `NN-description/` folder
with its own `PLAN.md`, not in a later task of the same plan.

## Run it

1. Copy this example somewhere outside the ola repo so its runtime artifacts
   (commits, logs, worktrees) don't touch the repo:

   ```bash
   cp -r examples/dummy-project /tmp/ola-demo
   cd /tmp/ola-demo
   git init dummy-project             # the project repo must be a git repo
   ```

2. Configure an agent. For OpenHands/Codex, copy the env template and fill it in:

   ```bash
   cp agent/.env.example agent/.env   # then edit agent/.env (never commit it)
   ```

   For Claude Code with a subscription, just have `claude` installed and logged in.

3. From the project repo, run a phase (use `--skip-sandbox` only when running
   on the host without a Docker sandbox):

   ```bash
   cd dummy-project
   ola -a cc --skip-sandbox            # processes 01-find-date, 02-utils, 03-parallel in order
   ```

4. Watch progress in another terminal:

   ```bash
   ola-top -f ../agent
   ```

The `03-parallel` folder ships a `.ola/concurrency` of `2`, so its tasks run two
at a time — expand it in `ola-top` to see the per-task view. Change the number
(or `echo 3 > agent/03-parallel/.ola/concurrency`) at any time; the cap is
re-read live.

## Blocked tasks and the janitor

If an agent cannot complete a task for out-of-scope reasons (say a task needs
an API key that isn't configured), it runs the `ola-blocked` script that ola
provisions into its worktree (the shipped `TASK-PROMPT.md` tells it how) and
stops. The task turns `blocked` (magenta in `ola-top`) and a **janitor** agent
is dispatched immediately: it either injects the missing prerequisite into the
live plan and defers the blocked task to a sibling `NNa-…-leftovers/` folder
that runs next, or — when only a human can help — files a
`NNb-…-blockers/BLOCKERS.md` that ola skips and reports at the end of the run.
To see it happen, add a task like `- [ ] Call the FOO API using FOO_API_KEY`
to a plan and run with a real agent.

> **Security note:** never put real API keys in `agent/.env.example` or commit
> `agent/.env`. The example ships only placeholders.
