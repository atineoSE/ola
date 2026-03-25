#!/usr/bin/env python3
"""Render a non-interactive preview of the ola-top table with fixture data.

Useful for iterating on table layout and styling without running the full TUI.
Renders collapsed, expanded, and narrow-terminal views.

Run with: PYTHONPATH=src python helper-scripts/render_preview.py
"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from ola.monitor.data import FolderStatus, IterationStatus
from ola.monitor.ui import build_table

# -- Fixture data -----------------------------------------------------------

FIXTURES = [
    FolderStatus(
        name="frontend",
        tasks_completed=3,
        tasks_total=5,
        iterations=[
            IterationStatus(
                phase="seed",
                wall_ms=45_000,
                input_tokens=12_500,
                output_tokens=3_200,
                cache_read_tokens=8_000,
            ),
            IterationStatus(
                phase="loop-1",
                wall_ms=32_000,
                input_tokens=15_000,
                output_tokens=4_100,
                cache_read_tokens=11_000,
            ),
        ],
    ),
    FolderStatus(
        name="backend-api",
        tasks_completed=7,
        tasks_total=7,
        iterations=[
            IterationStatus(
                phase="seed",
                wall_ms=120_000,
                input_tokens=250_000,
                output_tokens=45_000,
                cache_read_tokens=180_000,
            ),
            IterationStatus(
                phase="loop-1",
                wall_ms=80_000,
                input_tokens=300_000,
                output_tokens=52_000,
                cache_read_tokens=220_000,
            ),
            IterationStatus(
                phase="loop-2",
                wall_ms=60_000,
                input_tokens=310_000,
                output_tokens=38_000,
                cache_read_tokens=250_000,
            ),
        ],
    ),
    FolderStatus(
        name="database-migrations",
        tasks_completed=0,
        tasks_total=3,
        iterations=[
            IterationStatus(
                phase="seed",
                wall_ms=5_000,
                input_tokens=2_000,
                output_tokens=500,
                cache_read_tokens=100,
            ),
        ],
    ),
    FolderStatus(
        name="docs",
        tasks_completed=0,
        tasks_total=0,
        iterations=[],
    ),
]


def render_preview(
    width: int = 100,
    expanded: set[str] | None = None,
    cursor: int | None = 0,
) -> str:
    """Render the table to a string at the given terminal width."""
    table = build_table(
        FIXTURES,
        expanded=expanded or set(),
        cursor=cursor,
        agent_path=Path("/home/user/my-project/agent"),
    )
    buf = io.StringIO()
    console = Console(
        file=buf, width=width, force_terminal=True, color_system="truecolor"
    )
    console.print(table)
    return buf.getvalue()


if __name__ == "__main__":
    print("=" * 100)
    print("COLLAPSED VIEW (cursor on row 0)")
    print("=" * 100)
    print(render_preview(cursor=0))

    print("=" * 100)
    print("EXPANDED VIEW (backend-api expanded, cursor on row 1)")
    print("=" * 100)
    print(render_preview(expanded={"backend-api"}, cursor=1))

    print("=" * 100)
    print("NARROW TERMINAL (width=60)")
    print("=" * 100)
    print(render_preview(width=60, cursor=2))
