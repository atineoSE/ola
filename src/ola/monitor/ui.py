"""TUI rendering for ola-top using the rich library."""

from __future__ import annotations

import os
import select
import sys
import termios
import time as _time
import tty
from enum import Enum
from pathlib import Path

from rich.live import Live
from rich.table import Table
from rich.text import Text

from ola.monitor.data import FolderStatus, read_agent_folder


class ViewMode(Enum):
    """Display modes for the ola-top dashboard."""

    TASK = "task"
    METRICS = "metrics"


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


def _fmt_ratio(ratio: float) -> str:
    """Format an input/output token ratio for display."""
    if ratio == 0.0:
        return "-"
    if ratio >= 100:
        return f"{ratio:.0f}x"
    return f"{ratio:.1f}x"


def _fmt_tok_per_sec(tps: float) -> str:
    """Format tokens/second for display."""
    if tps == 0.0:
        return "-"
    if tps >= 100:
        return f"{tps:.0f}"
    return f"{tps:.1f}"


def _fmt_time_breakdown(breakdown: tuple[float, float]) -> str:
    """Format (llm_pct, tool_pct) as 'LL/TT'."""
    llm, tool = breakdown
    return f"{llm:.0f}/{tool:.0f}%"


def _cache_style(pct: float) -> str:
    """Return a color style based on cache hit rate percentage."""
    if pct >= 50:
        return "green"
    if pct >= 25:
        return "yellow"
    return "red"


def _find_active_index(folders: list[FolderStatus]) -> int | None:
    """Find the index of the currently-active folder.

    The active folder is the first one with incomplete tasks (has some work
    remaining). Returns None if no folder is active.
    """
    for idx, fs in enumerate(folders):
        if fs.tasks_total > 0 and fs.tasks_completed < fs.tasks_total:
            return idx
    return None


def build_table(
    folders: list[FolderStatus],
    expanded: set[str] | None = None,
    cursor: int | None = None,
    agent_path: Path | None = None,
    mode: ViewMode = ViewMode.TASK,
) -> Table:
    """Build a rich Table from a list of FolderStatus objects.

    Args:
        folders: List of folder statuses to display.
        expanded: Set of folder names whose iterations should be shown.
        cursor: Index of the currently highlighted folder (0-based), or None.
        agent_path: Path to the agent folder, shown in the header.
        mode: Which view mode to render (TASK or METRICS).
    """
    if expanded is None:
        expanded = set()

    active_idx = _find_active_index(folders)

    # Header: tool name, mode, agent path
    path_str = str(agent_path) if agent_path else ""
    mode_label = mode.value.upper()
    title = Text.assemble(
        ("ola-top", "bold cyan"),
        ("  ", ""),
        (f"[{mode_label}]", "bold magenta"),
        ("  ", ""),
        (path_str, "dim"),
    )

    # Footer: keybinding hints
    caption = Text.assemble(
        ("q", "bold"),
        (": quit  ", "dim"),
        ("m", "bold"),
        (": mode  ", "dim"),
        ("\u2191\u2193", "bold"),
        (": navigate  ", "dim"),
        ("Enter", "bold"),
        (": expand/collapse", "dim"),
    )

    table = Table(title=title, caption=caption, expand=True, show_header=True)
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Folder", style="bold")

    if mode == ViewMode.TASK:
        table.add_column("Agent", max_width=16, overflow="fold")
        table.add_column("Model", max_width=20, overflow="fold")
        table.add_column("Tasks", justify="right")
        table.add_column("Turns", justify="right")
        table.add_column("Time", justify="right")
    else:  # METRICS
        table.add_column("Input", justify="right")
        table.add_column("Output", justify="right")
        table.add_column("Avg Ctx", justify="right")
        table.add_column("Max Ctx", justify="right")
        table.add_column("Cache%", justify="right")
        table.add_column("In/Out", justify="right")
        table.add_column("LLM/Tool", justify="right")
        table.add_column("Tok/s", justify="right")
        table.add_column("Time", justify="right")

    for idx, fs in enumerate(folders):
        is_active = idx == active_idx

        # Determine row style based on task status
        if fs.tasks_total == 0:
            style = "dim"
        elif fs.tasks_completed >= fs.tasks_total:
            style = "green"
        elif is_active:
            style = "bold yellow"
        else:
            style = "yellow"

        # Highlight the cursor row
        is_cursor = cursor is not None and idx == cursor
        if is_cursor:
            style = f"reverse {style}" if style else "reverse"

        # Show expand indicator when there are iterations
        prefix = ""
        if fs.iterations:
            prefix = "\u25bc " if fs.name in expanded else "\u25b6 "

        # Active folder gets a marker
        active_marker = "\u25cf " if is_active else ""
        folder_cell = f"{active_marker}{prefix}{fs.name}"

        if mode == ViewMode.TASK:
            # Color tasks per-cell
            tasks_str = f"{fs.tasks_completed}/{fs.tasks_total}"
            if fs.tasks_total > 0 and fs.tasks_completed >= fs.tasks_total:
                tasks_text = Text(tasks_str, style="green")
            elif fs.tasks_total > 0:
                tasks_text = Text(tasks_str, style="yellow")
            else:
                tasks_text = Text(tasks_str, style="dim")

            turns_str = str(fs.total_num_turns) if fs.total_num_turns else ""
            table.add_row(
                str(idx + 1),
                folder_cell,
                fs.agent_display,
                fs.model_display,
                tasks_text,
                turns_str,
                _fmt_time(fs.total_wall_ms),
                style=style,
            )
        else:  # METRICS
            cache_pct_val = fs.cache_hit_rate
            cache_text = Text(
                f"{cache_pct_val:.0f}%", style=_cache_style(cache_pct_val)
            )

            table.add_row(
                str(idx + 1),
                folder_cell,
                _fmt_tokens(fs.total_input_tokens),
                _fmt_tokens(fs.total_output_tokens),
                _fmt_tokens(fs.avg_input_tokens),
                _fmt_tokens(fs.max_input_tokens),
                cache_text,
                _fmt_ratio(fs.io_ratio),
                _fmt_time_breakdown(fs.time_breakdown),
                _fmt_tok_per_sec(fs.llm_tok_per_sec),
                _fmt_time(fs.total_wall_ms),
                style=style,
            )

        # Render iteration sub-rows when expanded
        if fs.name in expanded:
            for it in fs.iterations:
                if mode == ViewMode.TASK:
                    delta = it.tasks_completed_delta
                    delta_str = str(delta) if delta else ""
                    it_turns_str = str(it.num_turns) if it.num_turns else ""
                    table.add_row(
                        "",
                        f"  \u2514 {it.phase}",
                        "",
                        "",
                        delta_str,
                        it_turns_str,
                        _fmt_time(it.wall_ms),
                        style="dim",
                    )
                else:  # METRICS
                    it_cache_val = it.cache_hit_rate
                    it_cache_text = Text(
                        f"{it_cache_val:.0f}%",
                        style=_cache_style(it_cache_val),
                    )
                    table.add_row(
                        "",
                        f"  \u2514 {it.phase}",
                        _fmt_tokens(it.input_tokens),
                        _fmt_tokens(it.output_tokens),
                        _fmt_tokens(it.avg_input_tokens),
                        _fmt_tokens(it.max_input_tokens),
                        it_cache_text,
                        _fmt_ratio(it.io_ratio),
                        _fmt_time_breakdown(it.time_breakdown),
                        _fmt_tok_per_sec(it.llm_tok_per_sec),
                        _fmt_time(it.wall_ms),
                        style="dim",
                    )

    return table


def _read_key(fd: int) -> str | None:
    """Read a single keypress without blocking. Returns None if no key is ready.

    Uses os.read() on the raw file descriptor so that select() and read
    operate on the same kernel buffer — Python's buffered sys.stdin.read()
    can desynchronise from select(), which caused escape sequences to be
    silently dropped.
    """
    if not select.select([fd], [], [], 0)[0]:
        return None
    data = os.read(fd, 1)
    if not data:
        return None
    if data == b"\x1b":
        # Escape sequences (e.g. arrow keys: \x1b[A).  Wait briefly for the
        # rest of the sequence, then read all available bytes in one shot.
        if select.select([fd], [], [], 0.1)[0]:
            data += os.read(fd, 16)
        return data.decode("utf-8", errors="replace")
    return data.decode("utf-8", errors="replace")


def run_live(agent_path: Path, refresh_interval: float = 2.0) -> None:
    """Run the live-updating TUI with keyboard controls."""
    print("\033[2J\033[H", end="", flush=True)  # clear screen, cursor to top
    expanded: set[str] = set()
    cursor = 0
    mode = ViewMode.TASK

    folders = read_agent_folder(agent_path)

    # Save terminal settings and switch to raw mode
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)

        with Live(
            build_table(folders, expanded, cursor, agent_path, mode),
            refresh_per_second=4,
        ) as live:
            last_refresh = _time.monotonic()
            while True:
                key = _read_key(fd)

                needs_update = False

                if key == "q" or key == "\x03":  # q or Ctrl-C
                    break
                elif key == "m":
                    mode = ViewMode.METRICS if mode == ViewMode.TASK else ViewMode.TASK
                    needs_update = True
                elif key == "\x1b[A":  # Up arrow
                    if folders and cursor > 0:
                        cursor -= 1
                        needs_update = True
                elif key == "\x1b[B":  # Down arrow
                    if folders and cursor < len(folders) - 1:
                        cursor += 1
                        needs_update = True
                elif key == "\r" or key == "\n":  # Enter
                    if folders:
                        name = folders[cursor].name
                        expanded ^= {name}
                        needs_update = True
                elif key and key.isdigit() and key != "0":
                    # Number keys 1-9 toggle that folder
                    idx = int(key) - 1
                    if 0 <= idx < len(folders):
                        cursor = idx
                        expanded ^= {folders[idx].name}
                        needs_update = True

                # Periodic data refresh
                now = _time.monotonic()
                if now - last_refresh >= refresh_interval:
                    folders = read_agent_folder(agent_path)
                    # Clamp cursor
                    if folders:
                        cursor = min(cursor, len(folders) - 1)
                    else:
                        cursor = 0
                    last_refresh = now
                    needs_update = True

                if needs_update:
                    live.update(
                        build_table(folders, expanded, cursor, agent_path, mode)
                    )

                _time.sleep(0.05)  # ~20 FPS input polling
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
