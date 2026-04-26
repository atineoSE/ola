"""Core outer loop logic."""

import json
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.plan import (
    count_tasks,
    discover_plan_folders,
    has_outstanding_tasks,
    read_file_if_exists,
)
from ola.stats import IterationStats, cache_hit_rate

_DEFAULT_LOOP_PROMPT = (
    Path(__file__).resolve().parent / "agents" / "DEFAULT-LOOP-PROMPT.md"
)

logger = logging.getLogger(__name__)

_MAX_STAGNANT_LOOPS = 5
_MAX_RATE_LIMIT_WAIT_SEC = 8 * 3600  # 8 hours — safety cap for rate-limit sleeps


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


def _git_commit(cwd: Path, message: str) -> None:
    """Stage all changes and commit. No-op if working tree is clean."""
    lock = cwd / ".git" / "index.lock"
    if lock.exists():
        logger.warning("Removing stale git lock file %s", lock)
        lock.unlink()
    _git(cwd, "add", "-A")
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=cwd, capture_output=True
    )
    if result.returncode != 0:  # there are staged changes
        _git(cwd, "commit", "-m", message)
        logger.info("Committed: %s", message)
    else:
        logger.debug("Nothing to commit after: %s", message)


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


def _last_loop_number(folder: Path) -> int:
    """Return the highest loop-N number from STATS.jsonl, or 0 if none."""
    stats_file = folder / "STATS.jsonl"
    if not stats_file.exists():
        return 0
    highest = 0
    for line in stats_file.read_text().strip().splitlines():
        try:
            phase = json.loads(line).get("phase", "")
        except (json.JSONDecodeError, AttributeError):
            continue
        if phase.startswith("loop-"):
            try:
                num = int(phase.split("-", 1)[1])
                highest = max(highest, num)
            except (ValueError, IndexError):
                continue
    return highest


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


def run_outer_loop(
    agent: Agent,
    plan_path: Path,
    limit: int | None = None,
) -> None:
    """Run the outer loop over plan subfolders."""
    # Load agent-folder .env (LLM_API_KEY, LMNR_*, etc.) before running agents
    env_file = plan_path / ".env"
    if env_file.is_file():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=True)
        logger.info("Loaded environment from %s", env_file)

    _ensure_git(plan_path)

    processed: set[Path] = set()
    while True:
        folders = discover_plan_folders(plan_path)
        remaining = [f for f in folders if f not in processed]
        if not remaining:
            if not processed:
                logger.info("No subfolders found in %s. Nothing to do.", plan_path)
            break
        for folder in remaining:
            logger.info("Processing: %s", folder.name)
            _process_folder(agent, folder, limit, plan_path)
            processed.add(folder)


def _process_folder(
    agent: Agent, folder: Path, limit: int | None, agent_root: Path
) -> None:
    """Process a single plan folder."""
    workdir = str(Path.cwd())

    # Create per-phase agent state directory
    state_dir: str | None = None
    if agent.state_dir_name:
        agent_state_path = folder / agent.state_dir_name
        agent_state_path.mkdir(parents=True, exist_ok=True)
        state_dir = str(agent_state_path)

    plan_file = folder / "PLAN.md"
    loop_prompt_file = folder / "LOOP-PROMPT.md"

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

    # Copy default loop prompt if none was provided
    if not loop_prompt_file.exists():
        import shutil

        shutil.copy2(_DEFAULT_LOOP_PROMPT, loop_prompt_file)
        logger.info("Copied DEFAULT-LOOP-PROMPT.md → %s", loop_prompt_file)

    loop_prompt = read_file_if_exists(loop_prompt_file)
    if loop_prompt is None:
        logger.warning("Skipping %s: no LOOP-PROMPT.md found.", folder.name)
        return

    # Inject absolute plan path so the agent can find it from the code dir
    effective_prompt = loop_prompt + f"\n\nPLAN.md is located at: {plan_file}"

    # Loop phase – resume numbering from STATS.jsonl so restarts don't
    # produce duplicate phase labels (e.g. a second "loop-1").
    iteration = _last_loop_number(folder)
    iterations_this_run = 0
    stagnant_iterations = 0
    while True:
        if not has_outstanding_tasks(folder):
            logger.info("No outstanding tasks in PLAN.md. Done with %s.", folder.name)
            break

        if limit is not None and iterations_this_run >= limit:
            logger.info(
                "Reached iteration limit (%d). Stopping %s.", limit, folder.name
            )
            break

        iteration += 1
        iterations_this_run += 1
        logger.info("Iteration %d%s...", iteration, f"/{limit}" if limit else "")

        tasks_before = count_tasks(folder)
        t0 = time.monotonic()
        labels = {"folder": folder.name, "phase": f"loop-{iteration}"}
        try:
            response = agent.run(
                effective_prompt, workdir, state_dir=state_dir, labels=labels
            )
        except KeyboardInterrupt:
            wall_ms = int((time.monotonic() - t0) * 1000)
            stats = IterationStats(
                error_type="interrupted",
                error_message="KeyboardInterrupt during iteration",
            )
            tasks_after = count_tasks(folder)
            _append_stats(
                folder,
                f"loop-{iteration}",
                stats,
                wall_ms,
                agent,
                tasks_before,
                tasks_after,
            )
            logger.info(
                "Interrupted during iteration %d of %s. Stats row written.",
                iteration,
                folder.name,
            )
            raise
        wall_ms = int((time.monotonic() - t0) * 1000)
        tasks_after = count_tasks(folder)
        label = f"LOOP #{iteration}"
        _log_response(label, response)
        _log_stats(label, response.stats, wall_ms)
        _append_stats(
            folder,
            f"loop-{iteration}",
            response.stats,
            wall_ms,
            agent,
            tasks_before,
            tasks_after,
        )

        # Rate-limit sleep-and-resume: don't treat as a fatal failure.
        if (
            response.stats.error_type == "rate_limited"
            and response.stats.rate_limit_resets_at
        ):
            wait_sec = max(0, response.stats.rate_limit_resets_at - time.time()) + 10
            if wait_sec > _MAX_RATE_LIMIT_WAIT_SEC:
                logger.error(
                    "Rate limit reset too far away (%ds). Stopping.", int(wait_sec)
                )
                break
            reset_ts = datetime.fromtimestamp(
                response.stats.rate_limit_resets_at
            ).isoformat(timespec="seconds")
            logger.warning(
                "Rate limit hit. Sleeping %ds until %s, then resuming %s.",
                int(wait_sec),
                reset_ts,
                folder.name,
            )
            try:
                time.sleep(wait_sec)
            except KeyboardInterrupt:
                logger.info("Sleep interrupted by user. Stopping %s.", folder.name)
                raise
            continue

        if not response.success:
            logger.error("Agent returned failure. Stopping %s.", folder.name)
            break

        # Stagnation backstop: break if the agent keeps succeeding but
        # makes no task progress, to avoid infinite loops from parser bugs.
        if tasks_after == tasks_before:
            stagnant_iterations += 1
        else:
            stagnant_iterations = 0

        if stagnant_iterations >= _MAX_STAGNANT_LOOPS:
            logger.warning(
                "No task progress for %d iterations in %s"
                " — breaking to avoid infinite loop. tasks=%s",
                _MAX_STAGNANT_LOOPS,
                folder.name,
                tasks_after,
            )
            break

        _git_commit(agent_root, f"ola: {folder.name} loop #{iteration}")


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
