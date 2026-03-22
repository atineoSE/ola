"""Core outer loop logic."""

import logging
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.plan import (
    discover_plan_folders,
    has_outstanding_tasks,
    read_file_if_exists,
)

logger = logging.getLogger(__name__)


def run_outer_loop(
    agent: Agent,
    plan_path: Path,
    limit: int | None = None,
) -> None:
    """Run the outer loop over plan subfolders."""
    folders = discover_plan_folders(plan_path)
    if not folders:
        logger.info("No subfolders found in %s. Nothing to do.", plan_path)
        return

    for folder in folders:
        logger.info("Processing: %s", folder.name)
        _process_folder(agent, folder, limit)


def _process_folder(agent: Agent, folder: Path, limit: int | None) -> None:
    """Process a single plan folder."""
    workdir = str(folder)

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

    # Seed phase: run SEED-PROMPT.md if it exists and PLAN.md doesn't yet
    seed_prompt = read_file_if_exists(folder / "SEED-PROMPT.md")
    if seed_prompt is not None:
        plan_exists = (folder / "PLAN.md").exists()
        if not plan_exists:
            logger.info("Running seed prompt...")
            plan_path = folder / "PLAN.md"
            seed_prompt += f"\n\nWrite your plan at {plan_path}"
            response = agent.run(seed_prompt, workdir, state_dir=state_dir)
            _log_response("SEED", response)
            if not response.success:
                logger.error("Seed prompt failed. Skipping folder.")
                return

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

        response = agent.run(loop_prompt, workdir, state_dir=state_dir)
        _log_response(f"LOOP #{iteration}", response)

        if not response.success:
            logger.error("Agent returned failure. Stopping %s.", folder.name)
            break


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
