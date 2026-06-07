# Dummy project — a runnable ola example

A minimal, **safe-to-run** example project that exercises the main ola features:
a seed phase, a sequential plan, and a parallel plan. The tasks are trivial
(find the date, write small files) so you can watch the harness work without a
large bill or a real codebase.

> These same scenarios are covered as hermetic, network-free tests in
> [`tests/e2e/`](../../tests/e2e/) — this folder is the human-facing companion
> you can actually run end to end with a real agent.

## Layout

```
dummy-project/
  src/                     # your "source code" repo (empty placeholder here)
  agent/
    .env.example           # copy to .env and fill in your provider
    01-find-date/          # SEED-PROMPT.md → agent generates PLAN.md, then runs it
    02-utils/              # a ready-made sequential PLAN.md
    03-parallel/           # a PLAN.md + .ola/concurrency=2 → runs tasks in parallel
```

## Run it

1. Copy this example somewhere outside the ola repo so its runtime artifacts
   (commits, logs, worktrees) don't touch the repo:

   ```bash
   cp -r examples/dummy-project /tmp/ola-demo
   cd /tmp/ola-demo
   git init src                       # ola requires src/ to be a git repo
   ```

2. Configure an agent. For OpenHands/Codex, copy the env template and fill it in:

   ```bash
   cp agent/.env.example agent/.env   # then edit agent/.env (never commit it)
   ```

   For Claude Code with a subscription, just have `claude` installed and logged in.

3. From `src/`, run a phase (use `--skip-sandbox` only when running on the host
   without a Docker sandbox):

   ```bash
   cd src
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

> **Security note:** never put real API keys in `agent/.env.example` or commit
> `agent/.env`. The example ships only placeholders.
