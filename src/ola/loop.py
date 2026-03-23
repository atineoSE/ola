"""Core outer loop logic."""

import json
import logging
import subprocess
import time
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.plan import (
    discover_plan_folders,
    has_outstanding_tasks,
    read_file_if_exists,
)
from ola.stats import IterationStats

logger = logging.getLogger(__name__)


def _ensure_git(cwd: Path) -> None:
    """Ensure a git repo exists in cwd; initialise one if not."""
    if not (cwd / ".git").exists():
        logger.info("Initialising git repository in %s", cwd)
        subprocess.run(["git", "init"], cwd=cwd, check=True, capture_output=True)
        _git_commit(cwd, "Initial commit")


def _git_commit(cwd: Path, message: str) -> None:
    """Stage all changes and commit. No-op if working tree is clean."""
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=cwd, capture_output=True
    )
    if result.returncode != 0:  # there are staged changes
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
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


def _log_stats(label: str, stats: IterationStats, wall_ms: int) -> None:
    """Log a one-liner with token usage and timing."""
    tokens = stats.input_tokens + stats.output_tokens
    if not tokens:
        return
    parts = [f"{_format_tokens(tokens)} tokens"]
    parts.append(f"{wall_ms / 1000:.0f}s")
    logger.info("[%s] %s", label, " · ".join(parts))


def _append_stats(
    folder: Path, label: str, stats: IterationStats, wall_ms: int
) -> None:
    """Append stats as a JSON line to STATS.jsonl in the phase folder."""
    record = {"phase": label, "wall_ms": wall_ms, **stats.model_dump()}
    stats_file = folder / "STATS.jsonl"
    with open(stats_file, "a") as f:
        f.write(json.dumps(record) + "\n")


def run_outer_loop(
    agent: Agent,
    plan_path: Path,
    limit: int | None = None,
) -> None:
    """Run the outer loop over plan subfolders."""
    _ensure_git(plan_path)

    folders = discover_plan_folders(plan_path)
    if not folders:
        logger.info("No subfolders found in %s. Nothing to do.", plan_path)
        return

    for folder in folders:
        logger.info("Processing: %s", folder.name)
        _process_folder(agent, folder, limit, plan_path)


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

    loop_prompt = read_file_if_exists(folder / "LOOP-PROMPT.md")
    if loop_prompt is None:
        logger.warning("Skipping %s: no LOOP-PROMPT.md found.", folder.name)
        return

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
            t0 = time.monotonic()
            response = agent.run(seed_prompt, workdir, state_dir=state_dir)
            wall_ms = int((time.monotonic() - t0) * 1000)
            _log_response("SEED", response)
            _log_stats("SEED", response.stats, wall_ms)
            _append_stats(folder, "seed", response.stats, wall_ms)
            if not response.success:
                logger.error("Seed prompt failed. Skipping folder.")
                return
            _git_commit(agent_root, f"ola: {folder.name} seed")

    # Inject absolute plan path so the agent can find it from the code dir
    effective_prompt = loop_prompt + f"\n\nPLAN.md is located at: {plan_file}"

    # Loop phase
    iteration = 0
    while True:
        if not has_outstanding_tasks(folder):
            logger.info("No outstanding tasks in PLAN.md. Done with %s.", folder.name)
            break

        if limit is not None and iteration >= limit:
            logger.info(
                "Reached iteration limit (%d). Stopping %s.", limit, folder.name
            )
            break

        iteration += 1
        logger.info("Iteration %d%s...", iteration, f"/{limit}" if limit else "")

        t0 = time.monotonic()
        response = agent.run(effective_prompt, workdir, state_dir=state_dir)
        wall_ms = int((time.monotonic() - t0) * 1000)
        label = f"LOOP #{iteration}"
        _log_response(label, response)
        _log_stats(label, response.stats, wall_ms)
        _append_stats(folder, f"loop-{iteration}", response.stats, wall_ms)

        if not response.success:
            logger.error("Agent returned failure. Stopping %s.", folder.name)
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
