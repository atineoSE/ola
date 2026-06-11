"""Core outer loop logic."""

import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ola.agents.base import Agent
from ola.plan import count_tasks, discover_plan_folders
from ola.stats import IterationStats

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


def _initial_concurrency(folder: Path, default: int | None = None) -> int:
    """Read the starting concurrency cap from ``<folder>/.ola/concurrency``.

    Returns *default* (the shared :data:`~ola.scheduler.DEFAULT_CONCURRENCY`
    when not overridden) if the file is missing or malformed. This supplies the
    scheduler's ``initial_cap``, which ``run_folder`` then materializes into the
    file on the first tick so the cap is always present on disk.
    """
    if default is None:
        from ola.scheduler import DEFAULT_CONCURRENCY

        default = DEFAULT_CONCURRENCY
    cap_file = folder / ".ola" / "concurrency"
    try:
        value = int(cap_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return default
    return value if value >= 1 else default


def _build_emitter(folder: Path) -> "Emitter":
    """Build the event emitter for a folder's parallel run.

    Attaches a :class:`~ola.events.client.LocalSink` writing to
    ``<folder>/.ola/events.jsonl`` — the folder's audit trail, and the source
    both ola-top and ola-dashboard read per-task progress from. Fire-and-forget.
    """
    from ola.events import Emitter, LocalSink

    return Emitter([LocalSink(folder / ".ola" / "events.jsonl")])


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
    # Imported here to avoid a module-level circular import (scheduler imports
    # loop). Raised below to stop the run when a folder can't be completed.
    from ola.scheduler import FolderIncompleteError

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

        # Completeness gate. _process_folder has drained the folder (its tasks
        # all ticked, relocated to a leftovers/blockers sibling, or exhausted
        # --max-attempts). "Checkbox is truth", so any checkbox still unticked
        # here is a task that could not be completed and was not relocated — the
        # folder is stuck. Bail out rather than advance past unfinished work.
        # (count_tasks returns (0, 0) for a PLAN-less folder, e.g. a blockers
        # folder, so those pass the gate and the loop skips them as before.)
        completed, total = count_tasks(folder)
        if completed < total:
            raise FolderIncompleteError(folder.name, total - completed)
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

    Hands every unchecked task in the folder's PLAN.md to the parallel
    scheduler. The old per-iteration inner loop is gone: task lifecycle, the
    stagnation backstop, and rate-limit sleep-and-resume now live in
    :mod:`ola.scheduler`.
    """
    # Imported here to avoid a circular import — scheduler imports loop for
    # per_task_state_dir.
    from ola.scheduler import run_folder

    # Create the folder-level agent state directory. The scheduler clones
    # per-task state dirs alongside it.
    if agent.state_dir_name:
        (folder / agent.state_dir_name).mkdir(parents=True, exist_ok=True)

    plan_file = folder / "PLAN.md"

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
