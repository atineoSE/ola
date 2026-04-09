"""TUI rendering for ola-top using the rich library."""

from __future__ import annotations

import os
import select
import shutil
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

# Terminal lines reserved for table chrome (title, borders, header, separator,
# bottom border, caption, plus a small safety margin for wrapped title text).
_TABLE_CHROME_ROWS = 8


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


def _fmt_ttft(ms: int, streamed: bool = True) -> str:
    """Format TTFT (time to first token) for display."""
    if not streamed:
        return "N/A"
    if ms == 0:
        return "-"
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


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


def _build_display_rows(
    folders: list[FolderStatus], expanded: set[str]
) -> list[tuple[str, int, int]]:
    """Flatten folders + expanded iterations into a single ordered list.

    Each entry is (kind, folder_idx, iter_idx). For folder rows iter_idx is -1.
    The order matches the visual order of the rendered table, so a flat index
    into this list directly addresses one row on screen.
    """
    rows: list[tuple[str, int, int]] = []
    for fi, fs in enumerate(folders):
        rows.append(("folder", fi, -1))
        if fs.name in expanded:
            for ii in range(len(fs.iterations)):
                rows.append(("iter", fi, ii))
    return rows


def _folder_row_index(
    rows: list[tuple[str, int, int]], folder_idx: int
) -> int:
    """Return the display row index of the given folder, or 0 if not found."""
    for ridx, row in enumerate(rows):
        if row[0] == "folder" and row[1] == folder_idx:
            return ridx
    return 0


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
    offset: int = 0,
    max_rows: int | None = None,
) -> Table:
    """Build a rich Table from a list of FolderStatus objects.

    Args:
        folders: List of folder statuses to display.
        expanded: Set of folder names whose iterations should be shown.
        cursor: Display row index of the highlighted row (0-based), or None.
            With expansion, this indexes into the flat list of folder + iter
            rows in visual order, so the cursor can land on iteration sub-rows.
        agent_path: Path to the agent folder, shown in the header.
        mode: Which view mode to render (TASK or METRICS).
        offset: Index of the first display row to render. Used for viewport
            scrolling so long iteration lists don't overflow the terminal.
        max_rows: Maximum data rows to render. None means render all rows
            (used by tests and one-shot output).
    """
    if expanded is None:
        expanded = set()

    active_idx = _find_active_index(folders)

    display_rows = _build_display_rows(folders, expanded)
    total_rows = len(display_rows)

    if max_rows is None:
        actual_offset = 0
        visible_rows = display_rows
    else:
        max_rows = max(1, max_rows)
        max_offset = max(0, total_rows - max_rows)
        actual_offset = max(0, min(offset, max_offset))
        visible_rows = display_rows[actual_offset : actual_offset + max_rows]

    # Header: tool name, mode, scroll indicator, agent path
    path_str = str(agent_path) if agent_path else ""
    mode_label = mode.value.upper()
    cursor_pos = (cursor + 1) if (cursor is not None and total_rows) else 0
    indicator = f"{cursor_pos}/{total_rows}"
    title = Text.assemble(
        ("ola-top", "bold cyan"),
        ("  ", ""),
        (f"[{mode_label}]", "bold magenta"),
        ("  ", ""),
        (indicator, "dim"),
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
        (": move  ", "dim"),
        ("PgUp/PgDn", "bold"),
        (": page  ", "dim"),
        ("g/G", "bold"),
        (": top/bot  ", "dim"),
        ("Enter", "bold"),
        (": expand", "dim"),
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
        table.add_column("TTFT", justify="right")
        table.add_column("Tok/s", justify="right")
        table.add_column("Time", justify="right")

    for vis_idx, (kind, fi, ii) in enumerate(visible_rows):
        flat_idx = actual_offset + vis_idx
        is_cursor = cursor is not None and flat_idx == cursor
        fs = folders[fi]

        if kind == "folder":
            is_active = fi == active_idx

            # Determine row style based on task status
            if fs.tasks_total == 0:
                style = "dim"
            elif fs.tasks_completed >= fs.tasks_total:
                style = "green"
            elif is_active:
                style = "bold yellow"
            else:
                style = "yellow"

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
                    str(fi + 1),
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
                    str(fi + 1),
                    folder_cell,
                    _fmt_tokens(fs.total_input_tokens),
                    _fmt_tokens(fs.total_output_tokens),
                    _fmt_tokens(fs.avg_input_tokens),
                    _fmt_tokens(fs.max_input_tokens),
                    cache_text,
                    _fmt_ratio(fs.io_ratio),
                    _fmt_time_breakdown(fs.time_breakdown),
                    _fmt_ttft(fs.total_ttft_ms, fs.all_streamed),
                    _fmt_tok_per_sec(fs.llm_tok_per_sec),
                    _fmt_time(fs.total_wall_ms),
                    style=style,
                )
        else:  # iter row
            it = fs.iterations[ii]
            iter_style = "reverse dim" if is_cursor else "dim"

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
                    style=iter_style,
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
                    _fmt_ttft(it.ttft_ms, it.streamed),
                    _fmt_tok_per_sec(it.llm_tok_per_sec),
                    _fmt_time(it.wall_ms),
                    style=iter_style,
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
    """Run the live-updating TUI with keyboard controls.

    Uses the alternate screen buffer (top-style) and a viewport-scrolled
    table so long iteration lists never overflow the terminal.
    """
    expanded: set[str] = set()
    cursor = 0  # display row index (folder + iter rows in visual order)
    offset = 0  # first display row currently visible
    mode = ViewMode.TASK

    folders = read_agent_folder(agent_path)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    def viewport_height() -> int:
        return max(
            1, shutil.get_terminal_size((80, 24)).lines - _TABLE_CHROME_ROWS
        )

    def clamp_view() -> None:
        """Keep cursor in bounds and scroll offset so cursor stays visible."""
        nonlocal cursor, offset
        rows = _build_display_rows(folders, expanded)
        total = len(rows)
        if total == 0:
            cursor = 0
            offset = 0
            return
        cursor = max(0, min(cursor, total - 1))
        max_r = viewport_height()
        if cursor < offset:
            offset = cursor
        elif cursor >= offset + max_r:
            offset = cursor - max_r + 1
        max_offset = max(0, total - max_r)
        offset = max(0, min(offset, max_offset))

    try:
        tty.setcbreak(fd)
        clamp_view()

        with Live(
            build_table(
                folders,
                expanded,
                cursor,
                agent_path,
                mode,
                offset=offset,
                max_rows=viewport_height(),
            ),
            refresh_per_second=4,
            screen=True,
        ) as live:
            last_refresh = _time.monotonic()
            last_size = shutil.get_terminal_size((80, 24))
            while True:
                key = _read_key(fd)
                needs_update = False

                if key == "q" or key == "\x03":  # q or Ctrl-C
                    break
                elif key == "m":
                    mode = (
                        ViewMode.METRICS if mode == ViewMode.TASK else ViewMode.TASK
                    )
                    needs_update = True
                elif key == "\x1b[A":  # Up arrow
                    if cursor > 0:
                        cursor -= 1
                        needs_update = True
                elif key == "\x1b[B":  # Down arrow
                    rows = _build_display_rows(folders, expanded)
                    if cursor < len(rows) - 1:
                        cursor += 1
                        needs_update = True
                elif key == "\x1b[5~":  # PgUp
                    cursor = max(0, cursor - viewport_height())
                    needs_update = True
                elif key == "\x1b[6~":  # PgDn
                    rows = _build_display_rows(folders, expanded)
                    cursor = min(
                        max(0, len(rows) - 1), cursor + viewport_height()
                    )
                    needs_update = True
                elif key == "g":  # Home
                    cursor = 0
                    needs_update = True
                elif key == "G":  # End
                    rows = _build_display_rows(folders, expanded)
                    cursor = max(0, len(rows) - 1)
                    needs_update = True
                elif key == "\r" or key == "\n":  # Enter
                    rows = _build_display_rows(folders, expanded)
                    if rows:
                        _, fi, _ = rows[cursor]
                        expanded ^= {folders[fi].name}
                        # Snap cursor back to the folder row so collapsing
                        # from inside an iter row doesn't dangle.
                        cursor = _folder_row_index(
                            _build_display_rows(folders, expanded), fi
                        )
                        needs_update = True
                elif key and key.isdigit() and key != "0":
                    # Number keys 1-9 jump to and toggle that folder
                    idx = int(key) - 1
                    if 0 <= idx < len(folders):
                        expanded ^= {folders[idx].name}
                        cursor = _folder_row_index(
                            _build_display_rows(folders, expanded), idx
                        )
                        needs_update = True

                # Periodic data refresh
                now = _time.monotonic()
                if now - last_refresh >= refresh_interval:
                    folders = read_agent_folder(agent_path)
                    last_refresh = now
                    needs_update = True

                # Repaint on terminal resize so the viewport tracks SIGWINCH
                current_size = shutil.get_terminal_size((80, 24))
                if current_size != last_size:
                    last_size = current_size
                    needs_update = True

                if needs_update:
                    clamp_view()
                    live.update(
                        build_table(
                            folders,
                            expanded,
                            cursor,
                            agent_path,
                            mode,
                            offset=offset,
                            max_rows=viewport_height(),
                        ),
                        refresh=True,
                    )

                _time.sleep(0.05)  # ~20 FPS input polling
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
