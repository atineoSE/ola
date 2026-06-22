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

Four agent backends are supported: **Claude Code** (`cc`), the **OpenHands SDK**
(`oh`), **Codex** (`cx`), and **Claude Code TUI** (`ct`).

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

### Layout

- `ola.sh` — the harness entrypoint (installed as the `ola` CLI via `uv tool install .`).
- `src/` — Python package.
- `dashboard/` — `ola-dashboard`, the browser-based monitor.
- `helper-scripts/`, `docker/`, `docs/`, `examples/`, `tests/` — supporting code, sandbox, docs, and the bats/pytest suites.
- `.claude/skills/` — the versioned skills (see below).

## Skills

Skills live under `.claude/skills/<name>/SKILL.md`. Each carries a `version` in
its frontmatter (semver, starting at `1.0.0`).

| Skill | Version | Purpose |
|-------|---------|---------|
| `ola-design` | 1.2.0 | Design philosophy and folder contract for the ola harness. Load whenever changing ola itself. |
| `ola-top` | 1.0.0 | Design philosophy and scope guardrails for ola-top, the zero-dependency terminal monitor. |
| `ola-dashboard` | 1.2.0 | Design philosophy and scope guardrails for ola-dashboard, the richer browser monitor. |
| `ola-plan` | 1.0.2 | Turn a settled plan into an ola agent-folder tree (numbered folders, parallel-safe tasks). |
| `codex` | 1.0.0 | Drive the Codex CLI headlessly against a replaceable model provider; parse its JSONL stream. |
| `openhands-sdk` | 1.1.0 | Configure the OpenHands SDK `LLM` and `Agent` classes. |
| `sbx` | 1.2.0 | Manage the Docker sandbox (`sbx` CLI) ola runs agents in: lifecycle (incl. killing in-sandbox processes), network policy, secrets, templates, resource limits (memory default + 75%-of-host hard cap + no-swap hard wall). Contract version-pinned; re-verify on sbx upgrade. |

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
