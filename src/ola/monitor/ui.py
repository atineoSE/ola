"""TUI rendering for ola-top using the rich library."""

from __future__ import annotations

from pathlib import Path

from rich.live import Live
from rich.table import Table

from ola.monitor.data import FolderStatus, read_agent_folder


def _fmt_tokens(n: int) -> str:
    """Format a token count for display (e.g. 1.2M, 45.3k)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_time(ms: int) -> str:
    """Format milliseconds as a human-readable duration."""
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins:02d}m"


def build_table(
    folders: list[FolderStatus],
    expanded: set[str] | None = None,
) -> Table:
    """Build a rich Table from a list of FolderStatus objects.

    Args:
        folders: List of folder statuses to display.
        expanded: Set of folder names whose iterations should be shown.
    """
    if expanded is None:
        expanded = set()

    table = Table(title="ola-top", expand=True)
    table.add_column("Folder", style="bold")
    table.add_column("Tasks", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cache%", justify="right")
    table.add_column("Time", justify="right")

    for fs in folders:
        # Determine row style based on task status
        if fs.tasks_total == 0:
            style = "dim"
        elif fs.tasks_completed >= fs.tasks_total:
            style = "green"
        else:
            style = "yellow"

        # Show expand indicator when there are iterations
        prefix = ""
        if fs.iterations:
            prefix = "▼ " if fs.name in expanded else "▶ "

        cache_pct = f"{fs.cache_hit_rate:.0f}%"
        table.add_row(
            f"{prefix}{fs.name}",
            f"{fs.tasks_completed}/{fs.tasks_total}",
            _fmt_tokens(fs.total_input_tokens),
            _fmt_tokens(fs.total_output_tokens),
            cache_pct,
            _fmt_time(fs.total_wall_ms),
            style=style,
        )

        # Render iteration sub-rows when expanded
        if fs.name in expanded:
            for it in fs.iterations:
                it_cache = f"{it.cache_hit_rate:.0f}%"
                table.add_row(
                    f"  └ {it.phase}",
                    "",
                    _fmt_tokens(it.input_tokens),
                    _fmt_tokens(it.output_tokens),
                    it_cache,
                    _fmt_time(it.wall_ms),
                    style="dim",
                )

    return table


def run_live(agent_path: Path, refresh_interval: float = 2.0) -> None:
    """Run the live-updating TUI."""
    with Live(
        build_table(read_agent_folder(agent_path)),
        refresh_per_second=1 / refresh_interval,
    ) as live:
        while True:
            import time

            time.sleep(refresh_interval)
            live.update(build_table(read_agent_folder(agent_path)))
