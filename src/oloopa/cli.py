"""CLI entry point for oloopa."""

import argparse
import logging
import sys
from pathlib import Path

from oloopa.agents import create_agent
from oloopa.loop import run_outer_loop

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="oloopa",
        description="Outer Loop of Agents",
    )
    parser.add_argument(
        "-a",
        "--agent",
        choices=["openhands", "oh", "claude-code", "cc"],
        default="cc",
        help="Agent to use (default: cc)",
    )
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        default=None,
        help="Model name (default: agent-specific default)",
    )
    parser.add_argument(
        "-p",
        "--plan",
        type=str,
        required=True,
        help="Path to the plan folder",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Max iterations per plan subfolder (default: no limit)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    plan_path = Path(args.plan).resolve()

    if not plan_path.is_dir():
        logger.error("%s is not a directory.", plan_path)
        sys.exit(1)

    agent = create_agent(args.agent, model=args.model)
    run_outer_loop(agent, plan_path, limit=args.limit)


if __name__ == "__main__":
    main()
