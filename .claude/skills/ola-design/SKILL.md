---
name: ola-design
description: Design philosophy and folder contract for the ola harness. Load whenever changing ola itself — every change must be checked against this philosophy.
version: 1.14.0
# 1.14.0: extend the one-detector item — ask whether the obvious wire is the
#          only one, and prefer a structured record (even one a CLI writes to
#          a file for its own reasons) over prose; keep the pin-and-log rule
#          for where prose really is all there is (minor: tightens 1.13.0,
#          no rule reversed).
# 1.13.0: extend the one-detector item to prose wires — pin each wording to a
#          capture, anchor on the condition's invariant rather than one
#          sentence, and log the screen that did not match (minor: tightens
#          1.7.0's guidance, no rule reversed).
# 1.9.0: sharpen the shared-resource item — the harness stops and lets the
#         supervisor wait, rather than sleeping in-process behind a duration
#         threshold (minor: tightens 1.8.0's guidance, no rule reversed).
# 1.8.0: add the shared-resource-stops-are-global checklist item — a dead
#         credential and an exhausted subscription window abort the run with
#         their own exit code and marker rather than failing task-by-task
#         (minor: new compatible guidance, no existing rule changed).
# 1.7.0: add the one-detector-per-condition / parse-the-wire-not-the-transcript
#         checklist item — a backend signal gets exactly one detector, read
#         from the stream shape the CLI actually emits (minor: new compatible
#         guidance, no existing rule changed).
# 1.6.0: add the agent-folder-declares-its-own-environment principle —
#         allowlist.txt, provision.sh and run-init.sh are agent-folder files
#         the harness only executes, so the released image stays generic and
#         workload-specific reclamation stays out of ola (minor: new
#         compatible guidance, no existing rule changed).
# 1.5.0: add the distinct-exit-code-per-terminal-state checklist item —
#         RunInterrupted/FolderIncompleteError/AuthEscalation each exit with
#         their own code so a caller can tell terminal run-states apart (minor:
#         new compatible guidance; the cc failure classification itself is
#         documented in CLAUDE.md, not duplicated here).
# 1.4.0: add the agent-commits-its-own-verified-work principle — the agent
#         commits in its worktree before ticking (project git hooks are its
#         gate, run in-loop), and every harness bookkeeping commit runs
#         --no-verify so a project hook never re-fires post-hoc (minor: new
#         compatible guidance, defers to CONTRACT.md for the load-bearing
#         detail).
# 1.3.0: add the merge-back-is-reconciliation principle — 3-way merge,
#         identical-add no-op, conflict → intra-run retry → janitor escalation,
#         no smart-merge in the hot path (minor: new compatible guidance, defers
#         to CONTRACT.md for the load-bearing detail).
# 1.2.0: add the project-repo/agent-folder separation, Ralph-minimal PLAN.md +
#         JANITOR-NOTES.md provenance, and explicit per-task tick-surface
#         principles to the philosophy and the change-checklist (minor: new
#         compatible guidance, no existing rule changed).
---

# The ola design philosophy

ola exists for **extreme autonomy and extreme parallelism, orchestrated
entirely through files**. The agent folder is the database: numbered folders
run in lexicographic order, the tasks inside one PLAN.md are independent and
parallel-safe, every task runs with a fresh context in its own worktree, and
a ticked checkbox is the only completion signal the harness accepts. When a
task cannot proceed, the harness unblocks it automatically (the janitor)
before ever waiting on a human.

**The agent folder declares its own environment.** The folder is the database
for *how the work runs*, not only for the work itself: `allowlist.txt` names the
egress the plan needs, `provision.sh` names the tooling it needs (applied by
the sandbox helpers on every create and reconnect), and `run-init.sh` names the
preconditions that must hold before dispatch (run by ola itself at startup). This is what keeps
the released image generic — a project that needs a database server or an extra
runtime declares it in its own folder instead of every unrelated sandbox
carrying the weight. Same discipline as the rest of the harness: declarative
file in the folder, applied mechanically, idempotent, and a failure refuses to
start rather than deferring the error into a task's logs.

The corollary is a boundary worth defending: when a task leaks state the
harness cannot name — a daemonized server, a lockfile, a cache — the fix is a
seam the project's own script fills, not workload knowledge inside ola. The
harness deletes worktrees; it does not learn what a worktree might have started.

**Two repos, one owner.** ola runs from inside the **project repo** — its
working directory is the project being worked on. The **agent folder** is the
project's ola-owned sibling and holds the plan database; the agent never touches
it. Per-task worktrees spawn from the *project* repo, so a task agent's working
directory is a project worktree of code only — it never sees the agent folder,
and everything it needs arrives in its prompt or is recoverable from the
project's current state. ola gives each task its **tick surface as an explicit
per-task path** — a per-task PLAN.md copy injected into the worktree — and
reconciles the tick back onto the agent-folder PLAN.md itself, so the agent
checks off a file it was handed rather than one it has to discover.

**Merging code back is reconciliation, not a hard gate.** A worktree branched
off an older project HEAD; folding it back is a 3-way merge (object-store
`git merge-tree`), so non-overlapping edits auto-resolve and a collision with an
already-present path — even an untracked, byte-identical one like a shared empty
`__init__.py` — reconciles instead of aborting the apply. A merge that still
conflicts is a non-stagnant *failed attempt*, retried within the run: the retry
re-branches off the updated HEAD and usually merges cleanly, so collisions
self-heal without new state. Only a conflict that outlasts `--max-attempts` is
durable coupling, and it escalates through the ordinary janitor/blockers path —
never a model-driven smart-merge in the hot path. (See CONTRACT.md.)

**PLAN.md stays Ralph-minimal.** A task is its description plus its genuine
dependencies and policies — never ola internals or self-referential priming.
When the janitor relocates a blocked task it writes that same minimal PLAN.md
(the task as an unchecked checkbox + real deps/policies) and parks the
why-blocked reason, verification, and provenance in a separate
`JANITOR-NOTES.md` sidecar — for human review only, never fed back to an agent.

## Canonical contract

The canonical contract text lives at `src/ola/agents/CONTRACT.md` — it is
also inlined into the janitor prompt at runtime, so it is load-bearing, not
just documentation. **Read that file before reasoning about folder, blocked,
janitor, leftovers, or blockers semantics.** This skill intentionally does
not duplicate it; keep it the single source of truth and update it (plus the
prompt templates that consume it) in the same change as any behavior change.

## Checklist when changing ola

Challenge every change against these questions; push back on the change if
the answer is wrong:

- Does it keep PLAN.md authoritative (checkbox-is-truth), or does it invent a
  second completion signal?
- Does it keep runtime state (`tasks.json` status/attempts, blocked markers,
  worktrees) **intra-run only**, so a fresh `ola` invocation re-derives every
  task from PLAN.md? A prior run's `failed`/`blocked` verdict or attempt count
  must never gate the next run — the dev re-runs ola repeatedly, fixing things
  between runs. Prior results persist solely as monitor history (events.jsonl /
  STATS.jsonl) on a read-only path that never feeds execution.
- Does anything new written **inside the agent folder** belong in the plan
  database, or is it runtime scratch that a wholesale `git add -A` would
  commit? The folder is committed on every tick and janitor pass, so a path
  ola writes there is a path ola *publishes*. Per-task backend state
  (`<folder>/.claude|.openhands|.codex/<task-id>/`) is the sharp case: it
  carries a live provider OAuth token plus megabytes of session logs. Such
  paths go in `.git/info/exclude` — ola's own bookkeeping, not the user's
  `.gitignore` — for **every** backend, not just the configured one, and
  already-committed ones get dropped from the index so an older folder heals
  on its next run (`_exclude_agent_state` in `loop.py`). Never mirror such a
  rule into the *project* repo's exclude: its `.claude/` is real source.
- Does it preserve task independence, or does it introduce coupling between
  tasks inside one plan? Dependent work belongs in a later folder.
- Does it keep orchestration file-driven and auditable, or does it move state
  into memory, flags, or hidden services?
- Does it require a human where a janitor (or a new escape hatch) could act?
  Prefer aggressive automatic unblocking; escalate via `…-blockers/BLOCKERS.md`
  only as a last resort.
- Does it preserve folder-ordering semantics, including letter-suffixed
  siblings (`01-init` → `01a-init-leftovers` → `01b-init-blockers` → `02-…`)
  and mid-run folder discovery?
- Does it keep worktrees spawning from the **project** repo, with the agent's
  cwd a project worktree and the agent folder ola-only — or does it leak
  agent-folder paths/internals into what the task can see?
- Does it keep PLAN.md **Ralph-minimal** (task + real dependencies/policies, no
  harness internals), routing any blocked reason and provenance to a separate
  `JANITOR-NOTES.md` sidecar — or does it push ola internals or why-blocked
  narration into PLAN.md itself?
- Does it hand the agent its **tick surface as an explicit per-task path** ola
  injects into the prompt, or does it make the agent discover which file to
  check off?
- Does it stay agent-backend-agnostic (Claude Code, OpenHands, Codex), or
  does it branch on a specific backend?
- Does it scale with parallelism (locks around shared PLAN.md/git state,
  live-read concurrency cap), or does it silently serialize work?
- Does it keep merging code back a **reconciliation** (3-way merge, identical-add
  no-op, conflict → intra-run retry → janitor escalation), or does it make a
  recoverable collision a hard failure — or worse, smart-merge with a model in
  the hot path?
- Does it keep the **agent's own worktree commit the project's quality gate**
  (project git hooks run in-loop, where the agent can fix and re-commit), with
  every harness bookkeeping commit `--no-verify` — or does it let a project
  hook re-fire on ola's reconciliation/tick commit, turning a post-agent
  bookkeeping step into a hidden hard gate the agent can never react to?
- Does each backend condition have **exactly one detector, reading the wire
  shape the CLI actually emits** — or does it grow a second transport for the
  same condition, and parse the persisted *transcript*'s field names instead
  of the *stream*'s? The two are different serializations (`isApiErrorMessage`
  on disk vs `is_api_error_message` on the wire), so a transcript-shaped
  detector is dead code that a transcript-shaped fixture will happily prove
  green. Corollary: once a condition's own event has fired, classify on it —
  don't second-guess it with a downstream field the CLI never promised to set
  (see the `rate_limit_event`-not-`result.subtype` rule in CLAUDE.md). This
  matters most where the stop manifests as **silence**: an end-of-turn
  heuristic built on quiescence cannot tell "finished" from "killed mid-turn",
  so a condition that goes quiet must be detected explicitly or it is silently
  reclassified as success-without-a-tick, i.e. stagnation.
  Then ask the harder question: **is the obvious wire the only one?** A UI
  backend looks like it forces prose — `ct` scraped screen banners for a year
  on that assumption — but the CLI was appending a structured record
  (`isApiErrorMessage`, `error`, `quotaLimits.resetsAt`) to its transcript the
  whole time, live, half a second after each failed request. Prose cost a run
  to find out: every marker required the word "reached", the CLI said "hit",
  and two folders burned their attempts against a wall that was already
  reported in a field. Prefer the structured wire even when it is somewhere
  unglamorous — a file the CLI writes for its own reasons — and prefer it for
  the second-order reasons too: an epoch beats an hour in a sentence (no
  parsing, no invented fallback grid), and a `status: "rejected"` field beats
  inferring a stop from a verb, since only the field can separate the CLI's own
  report from an agent *writing about* the same condition.
  Where prose really is all there is, pin every wording to a real capture and
  anchor on the invariant, not the sentence — and either way, log the input
  that matched nothing: a missed wording or a renamed field fails *silently*,
  and ola's `_run_tui` end-of-turn tail is the only reason the "hit" wording
  was ever found.
- When a stop is **global by nature** — one shared resource behind every task,
  like the credential or the subscription window — does it abort the run once,
  or fail task-by-task? Requeuing against an unmoved wall burns every task's
  `--max-attempts` in seconds and trips the folder's stagnation breaker, which
  reads as "the agent is stuck" when nothing is wrong with the agent. Abort
  with a distinct exit code, record the other in-flight tasks as failed, and
  leave a host-visible marker under `<agent-folder>/monitor/` carrying whatever
  the host needs to resume unattended (`ola-monitor` heals a credential, waits
  out a window). Keep the marker flat and grep-readable — the watcher is shell.
- Does the harness **stop and let the supervisor wait**, or does it park
  in-process? ola runs the plan; it does not sit on a clock. A process asleep
  for hours holds worktrees, a sandbox and a thread pool for nothing, and there
  is no in-flight work to protect when every task faces the same wall — while a
  relaunch re-derives the plan from PLAN.md, which is the ordinary resume path.
  Beware especially a *duration threshold* that picks between waiting and
  stopping: the constant is always arbitrary and the rare branch is the
  untested one. One condition, one reaction.
  The rule's premise is that the in-flight turn is **dead** — nothing to
  protect, so parking buys nothing. Check that premise per backend before
  applying it: `ct` drives the interactive CLI, which does *not* kill a
  rate-limited turn but parks it ("continuing automatically at 4pm") and resumes
  with its context intact, so there stopping would destroy live work to
  re-derive an unchanged plan, and `ct` waits instead. That is not the banned
  threshold — the branch is the CLI *stating its own intent*, a fact read off
  the wire, not a constant the harness picked. Where the CLI states nothing
  (no parsable reset), `ct` derives the next five-hour window boundary rather
  than stopping — a wrong guess is recoverable there because waking early only
  costs polling, so the usual "don't invent a duration" caution is outweighed.
  Removing a reaction removes its plumbing too: `ct` no longer raises the
  rate-limit escalation at all, and the branch was deleted rather than left
  behind as something that can never fire.
- Does it give each distinct terminal run-state its own process exit code
  (generic `1`, `128+signum` operator-interrupt, `40` Claude Code
  auth-escalation, `41` rate-limit escalation — see `RunInterrupted`/
  `FolderIncompleteError`/`AuthEscalation`/`RateLimitEscalation` in
  `src/ola/scheduler.py`) so a caller can tell them apart
  programmatically, or does it collapse a new failure mode into a generic
  non-zero exit?
