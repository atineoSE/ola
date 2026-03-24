"""CLI entry point for ola-top."""

from __future__ import annotations

import argparse
from pathlib import Path

from ola.monitor.ui import run_live


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and launch the TUI."""
    parser = argparse.ArgumentParser(
        prog="ola-top",
        description="Live terminal dashboard for monitoring OLA agent progress.",
    )
    parser.add_argument(
        "-f",
        "--agent-folder",
        type=Path,
        default=Path("../agent"),
        help="Path to the agent folder (default: ../agent)",
    )
    parser.add_argument(
        "-r",
        "--refresh",
        type=float,
        default=2.0,
        help="Refresh interval in seconds (default: 2)",
    )
    args = parser.parse_args(argv)
    run_live(args.agent_folder.resolve(), refresh_interval=args.refresh)
