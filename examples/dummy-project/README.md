# Dummy project — a runnable ola example

A minimal, **safe-to-run** example project that exercises the main ola features:
four indexed plan folders run in order, with the last one running its tasks in
parallel. The tasks are small and cheap (record the date, fetch one doc, send
one chat message) so you can watch the harness work without a large bill or a
real codebase. `02-get-docs` deliberately reaches the network through the
sandbox allowlist, and the dependent `03-cohere-chat` then trips the
**janitor**, so a real run shows both off.

> These folders double as the e2e scenarios: the hermetic, network-free suite
> in [`tests/e2e/`](../../tests/e2e/) copies the numbered folders verbatim and
> drives the real pipeline over them with a stub agent. The stub ticks every
> task, so the suite stays green and offline even though `03-cohere-chat`'s
> task blocks under a *real* agent. If you edit this example, run
> `make test-e2e` — the tests keep it from silently rotting, and this folder
> stays the human-facing version you can run with a real agent.

## Layout

```
dummy-project/             # workspace root
  dummy-project/           # the project repo — your "source code" (empty placeholder here)
  agent/
    .env.example           # copy to .env and fill in your provider
    allowlist.txt          # extra sandbox domains (opens *.cohere.com for the Cohere stages)
    01-find-date/          # a ready-made PLAN.md, run at the default cap of 1
    02-get-docs/           # fetches a doc over the network through the allowlist
    03-cohere-chat/        # uses that doc to call the API, then blocks → janitor
    04-parallel/           # a PLAN.md + .ola/concurrency=2 → runs tasks in parallel
```

The layout illustrates ola's ordering contract: tasks **within** a `PLAN.md`
are implicitly independent and parallel-safe (the concurrency cap only decides
how many run at once), while ordering between dependent stages comes from the
indexed folders — `01-find-date` completes before `02-get-docs` starts, and
`03-cohere-chat` builds on the doc `02-get-docs` fetched, and so on.
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
   ola -a cc --skip-sandbox            # processes 01-find-date, 02-get-docs, 03-cohere-chat, 04-parallel in order
   ```

4. Watch progress in another terminal:

   ```bash
   ola-top -f ../agent
   ```

The `04-parallel` folder ships a `.ola/concurrency` of `2`, so its tasks run two
at a time — expand it in `ola-top` to see the per-task view. Change the number
(or `echo 3 > agent/04-parallel/.ola/concurrency`) at any time; the cap is
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

`03-cohere-chat` ships this scenario built in: its task asks a real agent to
call the Cohere Chat API, which needs a `COHERE_API_KEY` you haven't set. The
agent blocks, and since only a human can supply the key, the janitor escalates
to a `03b-cohere-chat-blockers/BLOCKERS.md`. Run `03-cohere-chat` with a real
agent to watch it happen (the network-free e2e stub just ticks the task instead).

> **Security note:** never put real API keys in `agent/.env.example` or commit
> `agent/.env`. The example ships only placeholders.
