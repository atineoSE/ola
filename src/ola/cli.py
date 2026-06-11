"""CLI entry point for ola."""

import argparse
import logging
import sys
from importlib.metadata import version
from pathlib import Path

from ola.agents import create_agent
from ola.loop import run_outer_loop
from ola.sandbox import is_sandbox

logger = logging.getLogger(__name__)


def _env_command(argv: list[str]) -> int:
    """`ola env` — host-side: validate the agent .env's host-sourced
    ${VAR} refs (fail-fast) and print the fully-resolved KEY="VALUE"
    snapshot to stdout. Consumed by `ola-sandbox` to set the network
    policy and to inject a resolved sidecar into the sandbox."""
    parser = argparse.ArgumentParser(
        prog="ola env",
        description="Validate & resolve the agent .env (host-side).",
    )
    parser.add_argument(
        "-f",
        "--agent-folder",
        type=str,
        default="../agent",
        help="Path to the agent folder (default: ../agent)",
    )
    args = parser.parse_args(argv)

    from ola.envresolve import MissingHostVars, format_sidecar, resolved_values

    env_file = Path(args.agent_folder).resolve() / ".env"
    if not env_file.is_file():
        # No .env: agents fall back to the ambient environment. Nothing to
        # resolve and nothing to fail on.
        return 0
    try:
        resolved = resolved_values(env_file)
    except MissingHostVars as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for line in format_sidecar(resolved):
        print(line)
    return 0


def main() -> None:
    if sys.argv[1:2] == ["env"]:
        sys.exit(_env_command(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        prog="ola",
        description="Outer Loop of Agents",
    )
    parser.add_argument(
        "-a",
        "--agent",
        choices=["openhands", "oh", "claude-code", "cc", "codex", "cx"],
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
        "-f",
        "--agent-folder",
        type=str,
        default="../agent",
        help="Path to the agent folder (default: ../agent)",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Max iterations per plan subfolder (default: no limit)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Run a failed/stagnant task up to K times total (default: 3)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Disable debug logging",
    )
    parser.add_argument(
        "--no-janitor",
        action="store_true",
        help="Disable the janitor: blocked tasks stay blocked instead of"
        " triggering an automatic unblock/escalate run",
    )
    parser.add_argument(
        "--skip-sandbox",
        action="store_true",
        help="Allow running outside a Docker sandbox",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('ola')}",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.quiet else logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not is_sandbox() and not args.skip_sandbox:
        logger.error(
            "ola is not running inside a sandbox. "
            "Use --skip-sandbox to run outside a sandbox environment."
        )
        sys.exit(1)

    plan_path = Path(args.agent_folder).resolve()

    if not plan_path.is_dir():
        logger.error("%s is not a directory.", plan_path)
        sys.exit(1)

    agent = create_agent(args.agent, model=args.model)
    run_outer_loop(
        agent,
        plan_path,
        limit=args.limit,
        max_attempts=args.max_attempts,
        janitor_enabled=not args.no_janitor,
    )


if __name__ == "__main__":
    main()
