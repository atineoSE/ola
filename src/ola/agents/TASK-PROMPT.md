You have one task to complete. The task is: {{task_text}} (task id `{{task_id}}`). Do only this task and its automated tests, then verify. When finished, **tick this task's checkbox in PLAN.md** — your tick is the completion signal the harness uses to confirm success. Do not modify any other task's checkbox in PLAN.md.

If you cannot complete this task because something **out of scope** is missing (a prerequisite, a credential, an undecided design), do not guess and do not tick the checkbox. Instead run:

{{blocked_cmd}} --reason "one short sentence explaining what is missing"

and stop immediately. The harness will arrange unblocking.
