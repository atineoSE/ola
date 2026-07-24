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
reads the per-task transcript the TUI flushes on exit
(`<state_dir>/projects/.../<session>.jsonl`) and recovers token counts, turns,
models, and peak context (cost/cache-hit) — but **not** the streaming-only
timings (TTFT, decode-isolated tok/sec), which are never written to disk, and
nothing for a session too short to flush. Use `cc` when you need live timing;
use `ct` to drive the interactive UI with post-hoc token economics.

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
`_cc_clear_stale_keychain_entries` in `ola.sh`), and the `cc` backend deletes the
entry keyed to a task's config dir whenever it refreshes that dir's credentials
(prevention — `_clear_shadowing_keychain_entry` in `claude_code.py`). Both are
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

### `cc` failure classification

The `cc` backend (`src/ola/agents/claude_code.py`) differentiates three
failure modes instead of collapsing every non-200 into "authentication
failed":

- **Subscription limit** — sleep-and-resume, transparent to the plan. Two
  transports for the same condition, both routed into the same
  `error_type="rate_limited"` + `rate_limit_resets_at` path
  (`scheduler._run_with_rate_limit_resume`): (A) the structured
  `rate_limit_event` stream event (machine `resetsAt`), and (B) the
  *terminal* synthetic assistant message (`model:"<synthetic>"`,
  `error:"rate_limit"`, `isApiErrorMessage:true`, text like `"You've hit your
  session limit · resets 8:10pm (UTC)"`) — its reset time is prose-only and is
  parsed into an epoch (`_parse_session_limit_reset_epoch`).
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

The auth-escalation marker (`scheduler._write_auth_escalation_marker`) is
JSON at `<agent-folder>/monitor/auth-escalation.json`:
`{"sandbox": <name>, "ts": <ISO8601>, "message": <auth error>}`. The project
and agent folder are already bind-mounted into the sandbox, so the file is
host-visible with no new channel — this is the seam the host-side
`ola-monitor` watcher (below) polls.

### `ola-monitor` — host-side auth launcher-watcher

`ola-monitor` (in `ola.sh`, installed alongside `ola`/`ola-sandbox`) is
**not** the old in-sandbox progress monitor — that concept was scrapped (see
`agent/design-notes.md`); deterministic progress is `ola-top`'s job.
`ola-monitor`'s sole concern is auth recovery: only the host can run
`cc-credentials` against the Keychain and re-inject into the sandbox, so the
watcher runs on the host and launches `ola` *into* the sandbox, rather than
living in-sandbox. Invoke it with the same arguments you'd give `ola`:
`ola-monitor -a cc -f ../agent`. The sandbox to launch into isn't part of
`ola`'s own argv, so it's derived from the project checkout directory's
basename (override with `OLA_MONITOR_SANDBOX` if the sandbox was created
under a different name).

It: **(1) Launches** — ensures/creates the sandbox and injects fresh
credentials (`_ola_sandbox_prepare`, shared with `ola-sandbox`), then execs
`ola <args>` inside it non-interactively (`sbx exec`, not `sbx run`),
printing a one-line ack and otherwise passing ola's own logs through
untouched. **(2) Watches** the host-visible auth-escalation marker above.
**(3) Self-heals first** on the marker: re-pulls Keychain credentials
(`cc-credentials`) and re-injects them (`_ola_inject_credentials`), deletes
the marker, and relaunches `ola <args>` to resume the plan. **(4) Thrash
guards:** if the same account re-heals `OLA_MONITOR_THRASH_MAX` times
(default 3) within `OLA_MONITOR_THRASH_WINDOW` seconds (default 300) — the
signature of a concurrent rotator, e.g. a live `claude` session sharing the
account, which a mechanical re-pull cannot win — it stops re-healing and
notifies the human instead. **(5) Notifies** if `cc-credentials` finds no
valid Keychain token (user logged out) rather than looping. **(6) Exits**
with ola's own exit code once ola completes without dropping a marker.

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
| `ola-design` | 1.3.0 | Design philosophy and folder contract for the ola harness. Load whenever changing ola itself. |
| `ola-top` | 1.2.0 | Design philosophy and scope guardrails for ola-top, the zero-dependency terminal monitor. |
| `ola-dashboard` | 1.7.1 | Design philosophy and scope guardrails for ola-dashboard, the richer browser monitor. |
| `ola-plan` | 1.0.2 | Turn a settled plan into an ola agent-folder tree (numbered folders, parallel-safe tasks). |
| `codex` | 1.0.0 | Drive the Codex CLI headlessly against a replaceable model provider; parse its JSONL stream. |
| `openhands-cli` | 2.0.0 | Drive the OpenHands CLI headlessly as the `oh` backend: subprocess invocation, the `agent_settings.json` it loads, the `--JSON Event-` stream format, post-hoc metrics, and why not the (in-process-lock) SDK. |
| `sbx` | 2.3.0 | Manage the Docker sandbox (`sbx` CLI) ola runs agents in: lifecycle (incl. killing in-sandbox processes), network policy (incl. non-HTTP TCP / database egress via a bare-hostname allow rule), secrets, templates, resource limits (memory default + 75%-of-host hard cap + no-swap hard wall), host `gh` auth injection, the macOS per-config-dir Keychain shadowing gotcha (host-only), and `ola-monitor` (host-side auth launcher-watcher). Contract pinned to sbx v0.35.0; re-verify on sbx upgrade. |

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
