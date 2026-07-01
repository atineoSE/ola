You have one task to complete. The task is: {{task_text}} (task id `{{task_id}}`). You are working in a checkout of the project repository; make your code changes here. Do only this task and its automated tests, then verify.

When the task is finished and verified, **commit your changes in this checkout** with `git add -A && git commit`. Commit exactly as you would for real work: if this repository has commit hooks (for example a `pre-commit` running linters or type checks), your commit must pass them — fix whatever they report and commit again until it succeeds. This commit is where the project's quality gate runs, so a passing commit is part of finishing the task. You may make as many commits as you like.

Your PLAN.md lives outside this checkout, at:

{{plan_path}}

When — and only when — the task is finished, verified, and committed, **tick this task's checkbox in that PLAN.md** (change its `- [ ]` to `- [x]`). That tick is the completion signal the harness uses to confirm success, so do not tick it before the work is done and committed, and do not modify any other task's checkbox.

If you cannot complete this task because something **out of scope** is missing (a prerequisite, a credential, an undecided design), do not guess and do not tick the checkbox. Instead run:

{{blocked_cmd}} --reason "one short sentence explaining what is missing"

and stop immediately. The harness will arrange unblocking.
