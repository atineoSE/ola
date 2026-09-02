# CLAUDE.md

## What this repo is

**OLA (Outer Loop of Agents)** is a light harness for running AI coding agents on
long-horizon tasks. The whole system is driven by files: a folder structure plus
a few markdown files (`PLAN.md` per numbered subfolder) directs the work. It
follows the [Ralph Wiggum technique](https://ghuntley.com/ralph/) — the agent
iterates on fresh contexts, running relentlessly against the tasks in the plan.

The agent folder *is* the database: numbered folders run in lexicographic order,
the tasks inside one `PLAN.md` are independent and parallel-safe, each task runs
with a fresh context in its own worktree, and a ticked checkbox is the only
completion signal the harness accepts.

Four agent backends are supported: **Claude Code** (`cc`), the **OpenHands CLI**
(`oh`), **Codex** (`cx`), and **Claude Code TUI** (`ct`).

`oh` drives the standalone `openhands` CLI headlessly
(`openhands --headless --json --override-with-envs -f <task>`) in a subprocess —
one process per task, like `cc`/`cx`. This replaced an in-process OpenHands
**SDK** backend whose class-level lock serialized every LLM call to one in-flight
request per process (killing parallelism); subprocess isolation makes the fan-out
real. ola writes a per-task `agent_settings.json` (a serialized SDK `Agent`, full
LLM-knob fidelity) under a per-task `OPENHANDS_PERSISTENCE_DIR`, parses the CLI's
`--JSON Event-` stream for progress and the final message, and recovers token
economics post-hoc from the persisted `base_state.json`. Like `ct`, it has **no**
streaming-only timings (TTFT, decode-isolated tok/sec) — headless `--json`
carries no token-level chunks; it does get real per-call LLM latency. The CLI is
installed separately (`uv tool install openhands`), not as a Python dependency.
See `src/ola/agents/openhands.py` and the `openhands-cli` skill.

`ct` is an alternative Claude Code backend that drives the *interactive* `claude`
UI inside a pseudo-terminal instead of the headless `claude -p` stream. It exists
to exercise the real TUI form factor. The trade-offs are documented in
`src/ola/agents/claude_code_tui.py`: it detects end-of-turn from the screen going
idle (no result event), which is sound because the ticked PLAN.md checkbox is the
only completion signal the harness trusts (checkbox-is-truth). For metrics, `ct`
reads the per-task transcript at
`<state_dir>/projects/.../<session>.jsonl` (appended live, though metrics are
only *read* after `/exit`) and recovers token counts, turns,
models, and peak context (cost/cache-hit) — but **not** the streaming-only
timings (TTFT, decode-isolated tok/sec), which are never written to disk, and
nothing for a session too short to flush. Use `cc` when you need live timing;
use `ct` to drive the interactive UI with post-hoc token economics.

`ct` runs on the same subscription as `cc`, so the two global stops below apply
to it identically. The interactive TUI publishes no *stdout* stream, but it
does append a machine-readable record to the session transcript the moment a
request fails, and it appends it **live** — so `ct` reads both stops off that
file (`_TranscriptWatcher`, `parse_api_error`) rather than off the screen, and
raises them as exceptions that `run()` maps onto the same `error_type` the
scheduler keys on. The record is flagged `isApiErrorMessage: true` and carries
`error` (`rate_limit` / `authentication_failed`), `apiErrorStatus` (429 / —)
and, for a limit, a `quotaLimits` block with the same
`status` / `resetsAt` / `rateLimitType` payload `cc` gets from its
`rate_limit_event`. Note the wire/disk spelling difference still holds
(`is_api_error_message` on `cc`'s stream, `isApiErrorMessage` here) — same
condition, two serializations.

A rate limit in particular must not be left to the idle heuristic: a limited
turn goes silent immediately, so quiescence reads it as a finished turn that
merely failed to tick — stagnation, attempts burned against an unmoved wall,
the same stall `cc` hit until 2026-08.

**The screen is still the wire for state that never reaches a request** and so
writes no record: the trust dialog, the ready box, and a credential dead enough
that the TUI never opens a session (`is_auth_error`, consulted only in
`_await_ready`). That split is structural rather than a preference — before a
transcript exists there is nothing else to read, and once one exists it is the
only reader — so the two can never disagree about one event.

**A usage limit is where `ct` deliberately diverges from `cc`: it waits, in
process.** The interactive CLI does not kill a limited turn the way `claude -p`
does — the session and the context it has built survive the window. So `ct`
parks the turn until the record's own `quotaLimits.resetsAt`
(`_park_for_limit`) instead of escalating — an epoch the CLI states outright,
so there is nothing to parse and nothing to guess. This is an exception to
"ola never waits out a window", not an oversight: that rule assumes the turn
is *dead* and there is no in-flight work to protect, which is true of `cc` and
false here — and the signal that distinguishes them is the CLI reporting the
rejection itself, not a duration threshold ola invented.

**Waiting is only half of it: `ct` restarts the parked turn itself**
(`_nudge_after_limit` pastes `_RESUME_NUDGE` the moment the park ends). Claude
Code does have an auto-continue — it prints "continuing automatically at 4pm"
and re-sends the turn — but arming it requires the `autoContinueAtUsageLimit`
setting *and* a server-delivered config (`tengu_marble_heron`: `enabled`,
`autoArm`) that ola can neither read nor set, and the no-dialog arm is
additionally skipped in background/job contexts. The setting alone cannot force
it on: it is ANDed with both server flags, and it already defaults to *true*
when unset, so writing it changes nothing. Verified against a real session on
2026-09-02 — the CLI wrote its limit record, ended the turn, and sat at the
prompt until a human typed "continue".

ola does **not** try to work out which happened. "Did it resume by itself?" is
only answerable from silence — the very signal the end-of-turn heuristic reads
and the one thing that must not be trusted around a limit — and the nudge is
self-correcting either way: the CLI's own banner offers "esc **or type** to
cancel", so typing cancels a still-armed auto-continue and submits ola's prompt
instead (same outcome), while a nudge sent before the window truly reopened is
rejected, writes another limit record, and re-parks through the ordinary loop.
The asymmetry settles it: an unnecessary nudge costs one queued prompt, a
skipped one costs `_TURN_TIMEOUT_SEC` and the attempt. The nudge is the turn
loop's alone — `_await_ready` parks on the same records but has no turn to
restart, and pasting there would land in the input box ahead of the real
prompt.

When a limit record carries no usable `resetsAt` — never yet observed; all 34
captured records state one — or states a reset further out than a five-hour
window (`_MAX_PARK_SEC`, i.e. a weekly cap the TUI has never been seen sitting
through), `ct` falls back to the next five-hour **window boundary**
(`next_window_boundary`), a continuous grid stepping from one boundary this
account was observed to hit — not a daily
"every day at HH:00", since 24 is not a multiple of 5 and the wall-clock hour
therefore shifts an hour per day. Being wrong is survivable by construction:
waking early only costs a busier poll, because the park clears `saw_activity`
so the turn cannot end until the TUI actually produces output again; waking
late costs idle time bounded by one window — and a turn that wakes still
limited simply parks again on the next record. The anchor is an empirical fact
about one account (the window is *rolling*, anchored to first use after an idle
stretch), so `_WINDOW_ANCHOR_LOCAL` is where to correct the phase when it
drifts.

**`ct` therefore never raises a rate-limit escalation.** `error_type=
"rate_limited"`, `rate-limit.json` and exit 41 are now `cc`-only; the `ct` path
that used to raise them is gone rather than left as a branch that can no longer
fire. The residual risk is a limit the TUI does *not* self-continue (a weekly
cap, say): `ct` would park, wake, find itself still blocked, and eventually fail
the turn on `_TURN_TIMEOUT_SEC` — an ordinary non-stagnant failure that
requeues, not a fast global abort. If that shows up in practice, that is the
signal to bring a bounded escalation back.

Detection moved to the transcript on 2026-09-02, after a screen-scraped one
failed exactly the way prose fails. Every marker required the word "reached";
the CLI printed "You've hit your session limit · resets 11:10am (UTC)"; nothing
matched, so two plans' limited turns went silent, quiesced, and were read as
agents that finished without ticking — stagnant, attempts burned, both folders'
circuit breakers tripped ~13 minutes before the window reopened. The records
were on disk the whole time. Prose also forced a distinction the fields make
outright: the meter ("You've used 93% of your session limit") names the same
limit without hitting one, and writes no record at all, while the
`quotaLimits` `status` field says `rejected` in as many words.

Two properties are worth keeping in mind when changing this. `isApiErrorMessage`
is what separates the CLI's own report from an agent merely *writing about* a
rate limit — the runs above were building a dispatcher whose tests inject 429s,
so the string appears throughout their transcripts, and no screen match could
have told those apart. And the record shape is no more a public API than the
prose was, so `_run_tui` still logs the end-of-turn screen tail: a stop this
backend fails to recognise leaves no other trace, because such a turn reads as
finished-but-unticked and the scheduler drops `output` on that path.

### Claude Code credentials (`cc`/`ct`)

`cc`/`ct` authenticate with the host's Claude subscription. Credential handling
goes through the **`cc-credentials`** shell function (in `ola.sh`): it extracts
the live OAuth token from the macOS Keychain (`security find-generic-password -s
"Claude Code-credentials"`) into `~/.claude/.credentials.json`. `ola-sandbox`
runs `cc-credentials` on every sandbox create/reconnect, then injects the result;
the `cc` backend copies the bootstrap files into a per-task `CLAUDE_CONFIG_DIR`.
**Keep credentials fresh via `cc-credentials` — do not "fix" auth by pointing
`CLAUDE_CONFIG_DIR` at the live `~/.claude`; the per-task isolation is
intentional.** The host's `gh` (GitHub CLI) auth is injected into every
sandbox the same way, on the same create/reconnect path — see the `sbx`
skill's *Credentials* section.

One of those files is a `settings.json` **ola generates**
(`_ola_inject_cc_settings` in `ola.sh`) rather than copies from the host, which
would drag in personal hooks/MCP. It is the single place its keys are declared,
so it is copied into each per-task config dir on *every* run (it sits in
`_ALWAYS_REFRESH` alongside the credentials): per-task dirs are derived from the
task id and never deleted, so a copy-once file would pin a task to the settings
that existed the first time it ran. Three keys, and deliberately no `"sandbox"`
— Claude Code's own command sandbox is redundant inside the docker one and
confines writes to the worktree cwd, silently blocking the ola-blocked marker
that lands in the agent folder above it. The third is
`"disableRemoteControl": true`: an ola task agent is unattended, so
claude.ai/code, `--rc`, the auto-start and the in-session toggle have no
operator behind them (`remoteControlAtStartup: false` only stops the auto-start
and leaves the toggle live, so it is the wrong knob).

Gotcha (macOS host only): Claude Code also caches OAuth credentials **per
`CLAUDE_CONFIG_DIR`** in the Keychain, under
`Claude Code-credentials-<sha256(config_dir)[:8]>` — and that entry **outranks
the `.credentials.json` file inside that same dir**. ola's per-task config dirs
are derived from the task id, so they are stable across runs: a run whose token
expires mid-flight leaves a dead entry behind that poisons that task
*permanently*. The signature is a sub-second failure with
`Failed to authenticate: OAuth session expired and could not be refreshed`,
`duration_api_ms: 0` and no API call made — and `cc-credentials` alone cannot fix
it, because the file it refreshes is never read. Two automatic guards:
`cc-credentials` sweeps *expired* `Claude Code-credentials-*` entries (recovery;
the default entry and any live one are left alone —
`_cc_clear_stale_keychain_entries` in `ola.sh`), and the `cc` **and `ct`**
backends delete the entry keyed to a task's config dir whenever they refresh
that dir's credentials (prevention — `_clear_shadowing_keychain_entry` in
`claude_code.py`, called from `_run_once` and from `ct`'s `_build_env`; `ct`
builds the same task-id-derived config dirs, so it inherits the same trap).
Both are
no-ops without a `security` binary, so **the sandbox path is unaffected**: there
is no Keychain in the container and the injected file is already the sole
credential source. This only ever bit `ola --skip-sandbox`.

Gotcha: the subscription OAuth **refresh token rotates** on every refresh, so a
copied credential snapshot is invalidated the moment any *other* client sharing
the account refreshes. Running `ola -a cc` **from inside an active Claude Code
session** (which is itself refreshing that token) therefore yields spurious
`401 authentication_failed` (every task fails in ~1s). This is an environment
artifact, not a `cc` bug; the fix is to re-run `cc-credentials` (reconnect the
sandbox) and/or run ola outside a live `claude` session — not to bypass the
isolated config dir.

Gotcha: `sbx` injects placeholder provider API keys (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, etc. — the full list is `docker/placeholder-api-keys.txt`)
into every sandbox for generic downstream-tool support. Claude Code prefers
`ANTHROPIC_API_KEY` over its own OAuth `.credentials.json` whenever both are
present, so a live placeholder breaks the `cc` backend with `Invalid API key ·
Fix external API key` — a sub-second failure that looks identical to a dead
OAuth token but has nothing to do with credential freshness; re-running
`cc-credentials` does not touch it, because Claude Code never reads the file.
The sandbox image's `~/.bashrc` unsets these on every interactive login (see
the Dockerfile), which is why an interactive `ola-sandbox` attach followed by
`ola` at the prompt works even when the exact same run through `ola-monitor`
fails: `ola-monitor` launches `ola` via a non-interactive `sbx exec`, which
never sources `~/.bashrc`, so it strips the same keys itself with `env -u`
(reading the same tracked file, so the list is declared once — see
`_ola_placeholder_keys` in `ola.sh`). If you ever see `Invalid API key` instead
of an OAuth-shaped auth error, suspect this before suspecting the token.

Per-task config dirs live *inside the agent folder*
(`<numbered-folder>/.claude/<task-id>/`), which ola commits wholesale on every
tick and janitor pass — so the folder's `.git/info/exclude` carries every
backend's state-dir name (`.claude/`, `.openhands/`, `.codex/`), and any
already-committed ones are dropped from the index on the next run
(`loop._exclude_agent_state`). Without it the harness commits a live
`sk-ant-oat01-` token into the plan database. The rule is deliberately *not*
mirrored onto the project repo, whose own `.claude/` is tracked source; and it
is `info/exclude` rather than `.gitignore` because it is ola's bookkeeping, not
the user's. Excluding does not rewrite history — an agent folder that already
committed tokens keeps them in old commits, so purge before adding a remote.

### `cc`/`ct` failure classification

The `cc` backend (`src/ola/agents/claude_code.py`) differentiates three
failure modes instead of collapsing every non-200 into "authentication
failed". The wire shapes below are `cc`'s; `ct` reaches the same two
`error_type` values off the screen instead (above), and everything from
"escalates like auth" onward — scheduler abort, marker, exit code, who waits —
is shared, because it keys on `error_type` alone and never on the backend:

- **Subscription limit** — stop the run, let the supervisor wait. The signal is
  the structured `rate_limit_event` stream event with `status:"rejected"` and
  no fallback, which carries a machine `resetsAt`; it becomes
  `error_type="rate_limited"` + `rate_limit_resets_at`.
  **The event is the whole signal — never gate it on the `result` event.**
  The CLI has no rate-limit result subtype (the enum is `success |
  error_during_execution | error_max_turns | error_max_budget_usd |
  error_max_structured_output_retries`), so a turn killed by the limit still
  reports `subtype:"success"`, with `num_turns:1` and zero usage because the
  model never ran. Gating on the subtype (as ola did until 2026-08) makes
  every rejection look like an agent that succeeded without ticking its
  checkbox — stagnant, retries burned, folder circuit breaker tripped, minutes
  before the reset. `rejected` accompanies an actual 429, so it always means
  the turn was cut short.
  The CLI also emits a synthetic assistant message alongside it
  (`message.model:"<synthetic>"`, top-level `error:"rate_limit"` and
  `is_api_error_message:true`). ola deliberately does **not** parse it: it is
  the same condition, its reset time is prose only, and a second detector for
  one condition is a second thing to keep in sync. Note the wire spells that
  flag `is_api_error_message` while the on-disk transcript spells it
  `isApiErrorMessage` — an earlier detector was written against the transcript
  and so never once fired against a live stream. **Parse the stream shape, not
  the transcript shape**; they are different serializations.

  A limit escalates exactly like auth, and for the same reason: it is global —
  one subscription behind every task — so requeuing would burn every task's
  `--max-attempts` against a wall that has not moved. The scheduler aborts the
  run (`scheduler.RateLimitEscalation`), marks the other in-flight tasks
  failed, and the CLI exits **41** (see
  `scheduler.RATE_LIMIT_ESCALATION_EXIT_CODE`) — distinct from 40 because
  nothing is broken and nothing needs healing; the cure is time.

  **ola never waits out a window itself, at any duration.** Waiting is
  `ola-monitor`'s job. A process parked for hours holds worktrees, a sandbox
  and a thread pool for nothing; there is no in-flight work to protect, since
  every task is against the same wall; and a relaunch re-derives the plan from
  PLAN.md, which is the harness's ordinary resume path rather than a special
  one. ola *did* sleep in-process up to an 8h cap until 2026-08 — the cap was
  arbitrary, and having a threshold pick between two reactions to one condition
  is the same shape that produced the stall above. One condition, one reaction.
  Running bare `ola` (no watcher) therefore stops on a limit rather than
  sleeping through it: the log and exit code say when it resets, and re-running
  after that resumes from PLAN.md.
- **Authentication failure** — global, not per-task: one task's
  `authentication_error` means every task sharing that credential will fail
  the same way. `ClaudeCodeAgent.run()` still catches `AuthenticationError`
  (unchanged detection), but now tags the response
  `stats.error_type="authentication_error"`; the scheduler keys on that to
  abort the *entire* run rather than fail/requeue one task at a time
  (`scheduler.AuthEscalation`), marks every other in-flight task failed, drops
  the host-visible marker below, and the CLI exits with code **40** (see
  `scheduler.AUTH_ESCALATION_EXIT_CODE`) instead of the generic `1`. Recover
  by re-running `cc-credentials` (reconnect the sandbox) and re-running `ola`
  — PLAN.md state lets it resume.
- **Temporary failures** — anything else: normal per-task fail/requeue, no
  special handling.

Both escalations drop a host-visible marker under `<agent-folder>/monitor/`
(`scheduler._write_escalation_marker`). The project and agent folder are
already bind-mounted into the sandbox, so the file is host-visible with no new
channel — this is the seam the host-side `ola-monitor` watcher (below) polls,
and it is why the shape stays flat and grep-readable (the watcher is shell, no
jq):

- `auth-escalation.json` — `{"sandbox", "ts", "message"}`.
- `rate-limit.json` — `{"sandbox", "ts", "message", "resets_at"}`, where
  `resets_at` is the machine epoch or `null`. It is written by the worker
  rather than the main loop's flush, because the worker is the only place
  still holding that epoch: the outcome channel back to the loop is a plain
  string, and recovering the epoch from the human-facing message would mean
  re-parsing prose the CLI never promised.

### Per-project sandbox provisioning and run preconditions

The sandbox image stays generic: nothing that only one project needs is baked
into it. A project whose tasks need extra tooling puts a **`provision.sh` in
its agent folder**, and `_ola_provision` (in `ola.sh`) runs it inside the
sandbox on every create **and** reconnect, after credentials and before
attaching — symmetric with `allowlist.txt`, and aborting the prepare on a
non-zero exit like `_ola_apply_policy` does. The script is injected as base64
through `sbx exec` rather than executed by path, so it works even for an agent
folder outside the bind-mounted project dir.

No "already provisioned" marker is kept, deliberately: the script must be
idempotent, and a marker keyed to it would mask a broken internal fast-path
guard behind a cheap no-op. Two gotchas the contract calls out — sbx runs its
own `apt-get update` in the background on every sandbox *start* (so apt-based
provisioning wants `-o DPkg::Lock::Timeout=<n>`), and a `command -v` guard on
a binary the distro keeps off `PATH` never fires.

Its counterpart one level down is **`run-init.sh`**, in the same agent folder
but run by `ola` itself (`cli._run_init`, before `run_outer_loop`) rather than
by the sandbox helpers: `provision.sh` answers "what does this sandbox need
installed", `run-init.sh` answers "what must be true before the tasks start".
It runs on every run — including a re-run inside an already-attached sandbox,
which never goes through prepare — from the **project repo** as cwd, and aborts
the run on a non-zero exit. Sandbox-only: under `--skip-sandbox` it is skipped
with a warning, because pointing project shell code (typically a process sweep)
at the developer's own machine is exactly the wrong default.

The motivating case is reclaiming what a task leaked. A per-worktree server
daemonizes and reparents to PID 1, so nothing that kills a task agent, ola, or
the `sbx exec` ever stops it — and `worktree.cleanup()`/`create()` deleting the
worktree doesn't either (it runs on unlinked inodes). ola stays out of it: the
harness offers the seam, the project's script knows what to kill. Note this
reclaims at run *boundaries* only.

Relatedly, `_ola_sandbox_prepare`/`ola-sandbox` take the **agent folder as an
optional second argument** (default `../agent`, mirroring `cli.py`'s `-f`); a
project may hold one agent folder per epic. `ola-monitor` passes the folder it
already resolves from ola's own argv, so the sandbox is provisioned against
the plan being run.

### `ola-monitor` — host-side launcher-watcher

`ola-monitor` (in `ola.sh`, installed alongside `ola`/`ola-sandbox`) is
**not** the old in-sandbox progress monitor — that concept was scrapped (see
`agent/design-notes.md`); deterministic progress is `ola-top`'s job.
`ola-monitor`'s sole concern is keeping an unattended run going across the two
stops ola cannot clear from inside the sandbox: a dead credential (only the
host can run `cc-credentials` against the Keychain and re-inject) and an
exhausted subscription window (only wall-clock time clears it, and a sleeping
ola holding worktrees for hours is worse than a clean relaunch). That is why
the watcher runs on the host and launches `ola` *into* the sandbox, rather than
living in-sandbox. Invoke it with the same arguments you'd give `ola`:
`ola-monitor -a cc -f ../agent`. The sandbox to launch into isn't part of
`ola`'s own argv, so it's derived from the project checkout directory's
basename (override with `--monitor-sandbox NAME` if the sandbox was created
under a different name). `ola-monitor` strips its own flags
(`--monitor-sandbox`, `--monitor-thrash-max`, `--monitor-thrash-window`) out
of argv before forwarding the rest to `ola` unchanged — deliberately CLI
flags rather than env vars, so the knobs are visible in the invocation
itself instead of ambient shell state.

It: **(1) Launches** — ensures/creates the sandbox and injects fresh
credentials (`_ola_sandbox_prepare`, shared with `ola-sandbox`), then execs
`ola <args>` inside it non-interactively (`sbx exec`, not `sbx run`),
printing a one-line ack and otherwise passing ola's own logs through
untouched. **(2) Watches** the two host-visible markers above.
**(3) Waits out a rate limit** (`rate-limit.json`, checked first): sleeps to
the marker's `resets_at`, re-pulls credentials, deletes the marker, and
relaunches — ola re-derives every task from PLAN.md, so the plan resumes where
it stopped. The credential refresh is not optional: a five-hour window
comfortably outlives the OAuth token, and without it the relaunch would bounce
straight into an auth escalation. The wait is floored at 60s
(`_ola_monitor_rl_wait`) so a stale, missing or already-past `resets_at` can
never turn the relaunch into a hot spin. **(4) Self-heals auth**
(`auth-escalation.json`): re-pulls Keychain credentials (`cc-credentials`) and
re-injects them (`_ola_inject_credentials`), deletes the marker, and relaunches
`ola <args>`. **(5) Thrash guards** the *auth* path only: if the same account
re-heals `--monitor-thrash-max` times (default 3) within
`--monitor-thrash-window` seconds (default 300) — the signature of a concurrent
rotator, e.g. a live `claude` session sharing the account, which a mechanical
re-pull cannot win — it stops re-healing and notifies the human instead. A
repeated *rate-limit* wait is deliberately not guarded: a plan outliving
several windows is the normal case this exists for. **(6) Notifies** if
`cc-credentials` finds no valid Keychain token (user logged out) rather than
looping. **(7) Exits** with ola's own exit code once ola completes without
dropping a marker.

Both markers are cleared at startup if left over from a previous invocation.
For auth that stops a stale marker turning the first unrelated `sbx exec`
failure into a bogus escalation; for the rate limit the stakes are higher
still — the watcher would otherwise sleep towards an epoch that passed days
ago and relaunch a run that failed for an entirely different reason.

## Releases and the sandbox image

**`pyproject.toml`'s `version` is the only place a version is written.**
`ola --version` reads it via `importlib.metadata`; `ola.sh`'s `_ola_image_tag`
shells out to `ola --version` and resolves the sandbox template to
`ghcr.io/atineose/ola:<that version>` (`_ola_image_repo`, overridable with
`OLA_IMAGE_REPO`). So the CLI and the sandbox image it runs agents in are the
same release *by construction* — bumping `pyproject.toml` re-points both. Never
add a second place that spells out the version.

Image precedence in `_ola_sandbox_prepare`: `$OLA_SBX_IMAGE` → local `ola:dev`
(from `make sandbox-dev`) → `$OLA_IMAGE_REPO:$(ola --version)` → `:latest`
(fallback only, for a host with no `ola` on `PATH`). The `ola:dev` rung
deliberately outranks the released image so a developer's local build always
wins.

sbx pulls templates from an **OCI registry**, not the local Docker image store,
so publishing *is* pushing — `make release-image` builds `linux/amd64,linux/arm64`
(an arm64-only push from a Mac hard-fails `sbx create` on x86) and pushes both
`X.Y.Z` and `latest`. Releases are local and `make`-driven; there is no CI. Full
procedure in `.claude/skills/ola-release/SKILL.md`.

### Layout

- `ola.sh` — the harness entrypoint (installed as the `ola` CLI via `uv tool install .`); also provides the `ola-sandbox` and `ola-monitor` shell functions.
- `src/` — Python package.
- `dashboard/` — `ola-dashboard`, the browser-based monitor.
- `helper-scripts/`, `docker/`, `docs/`, `examples/`, `tests/` — supporting code, sandbox, docs, and the bats/pytest suites.
- `.claude/skills/` — the versioned skills (see below).

## Skills

Skills live under `.claude/skills/<name>/SKILL.md`. Each carries a `version` in
its frontmatter (semver, starting at `1.0.0`).

| Skill | Version | Purpose |
|-------|---------|---------|
| `ola-design` | 1.15.0 | Design philosophy and folder contract for the ola harness. Load whenever changing ola itself. |
| `ola-top` | 1.2.0 | Design philosophy and scope guardrails for ola-top, the zero-dependency terminal monitor. |
| `ola-dashboard` | 1.7.1 | Design philosophy and scope guardrails for ola-dashboard, the richer browser monitor. |
| `ola-plan` | 2.0.0 | Turn a settled plan into an ola agent-folder tree (numbered folders, parallel-safe tasks); the agent-folder `provision.sh`/`run-init.sh` seams and the long-lived-process rules, with example scripts; and the instructions-vs-data split — a task cannot open the agent folder, so the design is **inlined per stage into `TASK-PROMPT.md`** and never pointed at or committed to the project repo, while paths the *running code* opens must be in `HEAD` (`git cat-file -e HEAD:<path>`, not `git check-ignore`). |
| `ola-release` | 1.1.0 | Cut a release: bump `pyproject.toml`, publish the multi-arch sandbox image to GHCR, tag the repo. Load whenever releasing or changing how versions/images resolve. |
| `codex` | 1.0.0 | Drive the Codex CLI headlessly against a replaceable model provider; parse its JSONL stream. |
| `openhands-cli` | 2.0.0 | Drive the OpenHands CLI headlessly as the `oh` backend: subprocess invocation, the `agent_settings.json` it loads, the `--JSON Event-` stream format, post-hoc metrics, and why not the (in-process-lock) SDK. |
| `sbx` | 2.8.0 | Manage the Docker sandbox (`sbx` CLI) ola runs agents in: lifecycle (incl. killing in-sandbox processes, `prune`), network policy (incl. non-HTTP TCP / database egress via a bare-hostname allow rule, `--deny-network`), secrets (global-by-default scoping as of v0.39.0, dynamic/custom secrets), templates, resource limits (memory default + 75%-of-host hard cap + no-swap hard wall), host `gh` auth injection, the ola-owned Claude Code `settings.json` (no CC command sandbox, Remote Control hard-disabled, refreshed into every per-task config dir), the macOS per-config-dir Keychain shadowing gotcha (host-only), the background `apt-get update` sbx runs at every sandbox start, and `ola-monitor` (host-side launcher-watcher: auth healing *and* rate-limit waiting, incl. the agent-dir argument and `provision.sh` hook). Contract pinned to sbx v0.39.0; re-verify on sbx upgrade. |

## Treat skills as code

Skills are **source code, not scratch notes**. They are versioned and traceable
in version control like any other code in this repo:

- **Keep them current.** When you change behaviour the skill describes — the
  ola folder contract, a monitor's scope, an agent backend's invocation — update
  the matching `SKILL.md` in the same change. A stale skill is a bug.
- **Bump the `version`** on every meaningful edit, following semver: patch for
  clarifications and fixes, minor for new guidance that stays compatible, major
  for a change that contradicts prior guidance.
- **Commit skill changes alongside the code they describe**, with a message that
  explains *why*, so the skill's evolution is reviewable and traceable in git
  history.
