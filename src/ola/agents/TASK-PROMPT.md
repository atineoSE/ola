You have one task to complete. The task is: {{task_text}} (task id `{{task_id}}`). You are working in a checkout of the project repository; make your code changes here. Do only this task and its automated tests, then verify.

Your PLAN.md lives outside this checkout, at:

{{plan_path}}

When — and only when — the task is finished and verified, **tick this task's checkbox in that PLAN.md** (change its `- [ ]` to `- [x]`). That tick is the completion signal the harness uses to confirm success, so do not tick it before the work is done, and do not modify any other task's checkbox.

If you cannot complete this task because something **out of scope** is missing (a prerequisite, a credential, an undecided design), do not guess and do not tick the checkbox. Instead run:

{{blocked_cmd}} --reason "one short sentence explaining what is missing"

and stop immediately. The harness will arrange unblocking.
