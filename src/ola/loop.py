"""Core outer loop logic."""

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ola.agents.base import Agent, AgentResponse
from ola.plan import (
    count_tasks,
    discover_plan_folders,
    read_file_if_exists,
)
from ola.stats import IterationStats, cache_hit_rate

if TYPE_CHECKING:
    from ola.events import Emitter

logger = logging.getLogger(__name__)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run a git command, logging stderr on failure."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if result.returncode != 0:
        cmd = " ".join(["git", *args])
        logger.error("%s failed: %s", cmd, result.stderr.decode(errors="replace"))
        result.check_returncode()
    return result


def _ensure_git(cwd: Path) -> None:
    """Ensure a git repo exists in cwd; initialise one if not."""
    # Mark directory safe — mounted volumes have different ownership than the
    # container user, which makes git refuse to operate.
    _git(cwd, "config", "--global", "--add", "safe.directory", str(cwd))
    if not (cwd / ".git").exists():
        logger.info("Initialising git repository in %s", cwd)
        gitignore = cwd / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(".env\n")
        _git(cwd, "init")
        _git_commit(cwd, "Initial commit")
    _exclude_ola_artifacts(cwd)


def _exclude_ola_artifacts(cwd: Path) -> None:
    """Idempotently exclude ``.ola/`` runtime artifacts from git.

    Uses ``.git/info/exclude`` — shared by every worktree of the repo and
    invisible to the user's own ``.gitignore``. Without it the provisioned
    ``.ola/bin/ola-blocked`` script and other sidecar files would be swept
    into ``git add -A`` commits.
    """
    exclude = cwd / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    existing = exclude.read_text() if exclude.exists() else ""
    if ".ola/" in existing.splitlines():
        return
    with open(exclude, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(".ola/\n")


def _clear_lock(cwd: Path) -> None:
    lock = cwd / ".git" / "index.lock"
    if lock.exists():
        logger.warning("Removing stale git lock file %s", lock)
        lock.unlink()


def _git_commit(cwd: Path, message: str) -> None:
    """Stage all changes and commit. No-op if working tree is clean."""
    _clear_lock(cwd)
    result = subprocess.run(
        ["sh", "-c", 'git add -A && git commit -m "$1"', "_", message],
        cwd=cwd,
        capture_output=True,
    )
    if result.returncode == 0:
        logger.info("Committed: %s", message)
    elif result.returncode == 1 and b"nothing to commit" in result.stdout:
        logger.debug("Nothing to commit after: %s", message)
    else:
        logger.error("git commit failed: %s", result.stderr.decode(errors="replace"))
        result.check_returncode()


def _format_tokens(n: int) -> str:
    """Format token count as human-readable string."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _format_duration(ms: int) -> str:
    """Format milliseconds as human-readable duration."""
    secs = ms // 1000
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m{secs:02d}s"


def _log_stats(label: str, stats: IterationStats, wall_ms: int) -> None:
    """Log a one-liner with token usage and timing."""
    if not (stats.input_tokens or stats.output_tokens):
        return
    parts = []
    parts.append(f"in={_format_tokens(stats.input_tokens)}")
    parts.append(f"out={_format_tokens(stats.output_tokens)}")
    if stats.cache_read_tokens and stats.input_tokens:
        parts.append(
            f"cache={cache_hit_rate(stats.input_tokens, stats.cache_read_tokens):.0f}%"
        )
    if stats.ttft_ms:
        parts.append(f"ttft={stats.ttft_ms}ms")
    parts.append(_format_duration(wall_ms))
    logger.info("[%s] %s", label, " · ".join(parts))


def per_task_state_dir(folder: Path, agent: Agent, task_id: str) -> str | None:
    """Build the per-task agent state directory for parallel mode.

    Returns ``<folder>/<agent.state_dir_name>/<task_id>/`` as a string,
    creating parent directories as needed. Returns ``None`` for agents
    whose ``state_dir_name`` is empty (no state directory needed).
    """
    if not agent.state_dir_name:
        return None
    path = folder / agent.state_dir_name / task_id
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _initial_concurrency(folder: Path, default: int = 1) -> int:
    """Read the starting concurrency cap from ``<folder>/.ola/concurrency``.

    Returns *default* when the file is missing or malformed. This supplies the
    scheduler's ``initial_cap``; Phase 5 adds live re-reading of the same file
    on every scheduler tick.
    """
    cap_file = folder / ".ola" / "concurrency"
    try:
        value = int(cap_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return default
    return value if value >= 1 else default


def _build_emitter(folder: Path) -> "Emitter":
    """Build the event emitter for a folder's parallel run.

    Always attaches a :class:`~ola.events.client.LocalSink` writing to
    ``<folder>/.ola/events.jsonl`` (the folder's audit trail, also the source
    ola-top reads per-task progress from). When ``OLA_COLLECTOR_URL`` is set in
    the environment, an :class:`~ola.events.client.HttpSink` is added so events
    are also POSTed to a remote collector. Both sinks are fire-and-forget.
    """
    from ola.events import Emitter, HttpSink, LocalSink, Sink

    sinks: list[Sink] = [LocalSink(folder / ".ola" / "events.jsonl")]
    collector_url = os.environ.get("OLA_COLLECTOR_URL")
    if collector_url:
        try:
            sinks.append(HttpSink(collector_url))
            logger.info("Emitting events to collector at %s", collector_url)
        except Exception:  # noqa: BLE001 - never let event setup break a run
            logger.exception("Failed to set up HTTP event sink; continuing local-only")
    return Emitter(sinks)


def _append_stats(
    folder: Path,
    label: str,
    stats: IterationStats,
    wall_ms: int,
    agent: Agent | None = None,
    tasks_before: tuple[int, int] = (0, 0),
    tasks_after: tuple[int, int] = (0, 0),
) -> None:
    """Append stats as a JSON line to STATS.jsonl in the phase folder."""
    # Derive tool_ms from llm_ms when the agent provides LLM latency
    # but not tool timing (e.g. OpenHands reports llm_ms from API latencies).
    if stats.tool_ms == 0 and stats.llm_ms > 0:
        stats.tool_ms = max(0, wall_ms - stats.llm_ms)
    record = {"phase": label, "wall_ms": wall_ms, **stats.model_dump()}
    if agent is not None:
        record["agent"] = agent.mnemonic
        record["agent_version"] = agent.version()
    record["tasks_completed"] = tasks_after[0]
    record["tasks_total"] = tasks_after[1]
    record["tasks_completed_delta"] = tasks_after[0] - tasks_before[0]
    stats_file = folder / "STATS.jsonl"
    with open(stats_file, "a") as f:
        f.write(json.dumps(record) + "\n")


def _load_agent_env(plan_path: Path) -> None:
    """Load the agent .env before running agents.

    In a sandbox, prefer the host-resolved snapshot written by `ola-sandbox`
    (concrete values, no ${VAR} left). On the host, validate that every
    mandatory host-sourced ref is present before letting python-dotenv
    interpolate — the host environment must be sound before proceeding.
    """
    from dotenv import load_dotenv

    from ola.envresolve import MissingHostVars, validate
    from ola.sandbox import SIDECAR_ENV, is_sandbox

    env_file = plan_path / ".env"
    if is_sandbox() and SIDECAR_ENV.is_file():
        load_dotenv(SIDECAR_ENV, override=True)
        logger.info("Loaded resolved environment from %s", SIDECAR_ENV)
    elif env_file.is_file():
        try:
            validate(env_file)
        except MissingHostVars as exc:
            logger.error("%s", exc)
            if is_sandbox():
                logger.error(
                    "Inside a sandbox the resolved env is supplied by "
                    "`ola-sandbox`; reconnect via `ola-sandbox <name>` on "
                    "the host after fixing the host environment."
                )
            raise SystemExit(1) from exc
        load_dotenv(env_file, override=True)
        logger.info("Loaded environment from %s", env_file)


def run_outer_loop(
    agent: Agent,
    plan_path: Path,
    limit: int | None = None,
    max_attempts: int = 0,
    janitor_enabled: bool = True,
) -> None:
    """Run the outer loop over plan subfolders."""
    _load_agent_env(plan_path)

    _ensure_git(plan_path)

    # One folder per discovery pass: a janitor may create a letter-suffixed
    # sibling (e.g. 01a-init-leftovers) while 01-init is running, and it must
    # be picked up before 02-… — re-discovering after every folder makes the
    # lexicographic sort do the interleaving.
    processed: set[Path] = set()
    while True:
        folders = discover_plan_folders(plan_path)
        folder = next((f for f in folders if f not in processed), None)
        if folder is None:
            if not processed:
                logger.info("No subfolders found in %s. Nothing to do.", plan_path)
            break
        logger.info("Processing: %s", folder.name)
        _process_folder(agent, folder, limit, plan_path, max_attempts, janitor_enabled)
        processed.add(folder)

    _log_attention_summary(plan_path)


def _log_attention_summary(plan_path: Path) -> None:
    """Log a 'human attention needed' block for escalations and blocked tasks."""
    blockers = sorted(plan_path.glob("*/BLOCKERS.md"))
    blocked_tasks: list[str] = []
    for tasks_file in sorted(plan_path.glob("*/.ola/tasks.json")):
        try:
            raw = json.loads(tasks_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for entry in raw.get("tasks", []):
            if entry.get("status") == "blocked":
                blocked_tasks.append(
                    f"{tasks_file.parent.parent.name}: {entry.get('text', '?')}"
                    f" ({entry.get('last_error') or 'no reason recorded'})"
                )
    if not blockers and not blocked_tasks:
        return
    logger.warning("=== Human attention needed ===")
    for path in blockers:
        logger.warning("Escalated blockers: %s", path)
    for line in blocked_tasks:
        logger.warning("Blocked task — %s", line)


def _process_folder(
    agent: Agent,
    folder: Path,
    limit: int | None,
    agent_root: Path,
    max_attempts: int = 0,
    janitor_enabled: bool = True,
) -> None:
    """Process a single plan folder.

    Runs the optional seed phase, then hands every unchecked task in PLAN.md
    to the parallel scheduler. The old per-iteration inner loop is gone: task
    lifecycle, the stagnation backstop, and rate-limit sleep-and-resume now
    live in :mod:`ola.scheduler`.
    """
    # Imported here to avoid a circular import — scheduler imports loop for
    # per_task_state_dir.
    from ola.scheduler import run_folder

    workdir = str(Path.cwd())

    # Create the folder-level agent state directory. The seed phase uses it
    # directly; the scheduler clones per-task state dirs alongside it.
    state_dir: str | None = None
    if agent.state_dir_name:
        agent_state_path = folder / agent.state_dir_name
        agent_state_path.mkdir(parents=True, exist_ok=True)
        state_dir = str(agent_state_path)

    plan_file = folder / "PLAN.md"

    # Seed phase: run SEED-PROMPT.md if it exists and PLAN.md doesn't yet
    seed_prompt = read_file_if_exists(folder / "SEED-PROMPT.md")
    if seed_prompt is not None:
        if not plan_file.exists():
            logger.info("Running seed prompt...")
            seed_prompt += (
                f"\n\nWrite your plan at {plan_file}"
                " using markdown tasks, i.e. `- [ ] `"
            )
            tasks_before = count_tasks(folder)
            t0 = time.monotonic()
            labels = {"folder": folder.name, "phase": "seed"}
            response = agent.run(
                seed_prompt, workdir, state_dir=state_dir, labels=labels
            )
            wall_ms = int((time.monotonic() - t0) * 1000)
            tasks_after = count_tasks(folder)
            _log_response("SEED", response)
            _log_stats("SEED", response.stats, wall_ms)
            _append_stats(
                folder,
                "seed",
                response.stats,
                wall_ms,
                agent,
                tasks_before,
                tasks_after,
            )
            if not response.success:
                logger.error("Seed prompt failed. Skipping folder.")
                return
            _git_commit(agent_root, f"ola: {folder.name} seed")

    if not plan_file.exists():
        if (folder / "BLOCKERS.md").exists():
            logger.info(
                "Skipping %s: BLOCKERS.md present — awaiting human input.",
                folder.name,
            )
        else:
            logger.warning("Skipping %s: no PLAN.md found.", folder.name)
        return

    if limit is not None:
        logger.info(
            "--limit is ignored in parallel mode; task lifecycle is per-task,"
            " not per-iteration."
        )

    cap = _initial_concurrency(folder)
    logger.info("Dispatching tasks in %s (concurrency cap %d).", folder.name, cap)
    emitter = _build_emitter(folder)
    try:
        run_folder(
            agent,
            folder,
            agent_root,
            cap,
            emitter=emitter,
            max_attempts=max_attempts,
            janitor_enabled=janitor_enabled,
        )
    finally:
        emitter.close()


def _log_response(label: str, response: AgentResponse) -> None:
    """Log a truncated agent response."""
    status = "OK" if response.success else "FAIL"
    logger.info("[%s] %s", label, status)
    lines = response.output.strip().splitlines()
    if len(lines) <= 20:
        for line in lines:
            logger.debug("  %s", line)
    else:
        for line in lines[:10]:
            logger.debug("  %s", line)
        logger.debug("  ... (%d lines omitted) ...", len(lines) - 20)
        for line in lines[-10:]:
            logger.debug("  %s", line)
