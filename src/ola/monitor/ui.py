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
from typing import Any

from rich.console import Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ola.monitor.data import FolderStatus, read_agent_folder

# Terminal lines reserved for table chrome:
# - title (1)
# - top border (1)
# - header row (1)
# - separator between header and data (1)
# - bottom border (1)
# - caption (1)
# - blank line before caption (1)
# - the always-present TOTAL footer row (1) and the divider line above it (1)
_TABLE_CHROME_ROWS = 9


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


# Per-task status → row color for the parallel-mode expanded view.
_TASK_STATUS_STYLES: dict[str, str] = {
    "complete": "green",
    "running": "cyan",
    "failed": "red",
    "pending": "dim",
    "blocked": "magenta",
}


def _task_status_style(status: str) -> str:
    """Return a color style for a per-task row given its status."""
    return _TASK_STATUS_STYLES.get(status, "")


# Columns eligible to be the "active" detail column, per mode, in display order.
# "#" is omitted (it is just the row number). Left/Right cycles through these and
# the bottom detail line shows the active column's full value for the cursor row
# — the only indication of which column is active. Folder is the default.
_TASK_DETAIL_COLS: list[str] = ["Folder", "Agent", "Model", "Tasks", "Turns", "Time"]
_METRICS_DETAIL_COLS: list[str] = [
    "Folder", "Input", "Output", "Avg Ctx", "Max Ctx",
    "Cache%", "In/Out", "LLM/Tool", "TTFT", "Tok/s", "Time",
]

# The detail line never spans more than this many terminal lines; a value longer
# than that at the current width is clipped with an ellipsis, so the detail can
# never blow the viewport budget the way a folding grid cell once did.
_MAX_DETAIL_LINES = 3


def _detail_cols(mode: ViewMode) -> list[str]:
    """Column labels the detail line can cycle through, in display order."""
    return _TASK_DETAIL_COLS if mode == ViewMode.TASK else _METRICS_DETAIL_COLS


def _row_values(
    folders: list[FolderStatus], kind: str, fi: int, ii: int, mode: ViewMode
) -> dict[str, str]:
    """Plain-text value of every column for one display row.

    Single source of truth for cell text, shared by the grid (``build_table``
    styles and truncates these) and the bottom detail line (which shows the
    active column's full, untruncated value for the row under the cursor). The
    ``Folder`` value omits the expand-arrow prefix and the ``#`` value the row
    number adornments — ``build_table`` adds those affordances when it styles
    the row.
    """
    fs = folders[fi]
    if kind == "folder":
        if mode == ViewMode.TASK:
            return {
                "#": str(fi + 1),
                "Folder": fs.name,
                "Agent": fs.agent_display,
                "Model": fs.model_display,
                "Tasks": f"{fs.tasks_completed}/{fs.tasks_total}",
                "Turns": str(fs.total_num_turns) if fs.total_num_turns else "",
                "Time": _fmt_time(fs.display_wall_ms),
            }
        return {
            "#": str(fi + 1),
            "Folder": fs.name,
            "Input": _fmt_tokens(fs.total_input_tokens),
            "Output": _fmt_tokens(fs.total_output_tokens),
            "Avg Ctx": _fmt_tokens(fs.avg_input_tokens),
            "Max Ctx": _fmt_tokens(fs.max_input_tokens),
            "Cache%": f"{fs.cache_hit_rate:.0f}%",
            "In/Out": _fmt_ratio(fs.io_ratio),
            "LLM/Tool": _fmt_time_breakdown(fs.time_breakdown),
            "TTFT": _fmt_ttft(fs.median_ttft_ms, fs.all_streamed),
            "Tok/s": _fmt_tok_per_sec(fs.llm_tok_per_sec),
            "Time": _fmt_time(fs.display_wall_ms),
        }
    if kind == "task":
        tr = fs.task_rows[ii]
        label = f"  └ Task {ii + 1} ({tr.task_id})"
        folder_cell = f"{label}: {tr.text}" if tr.text else label
        st = tr.stats
        elapsed = _fmt_time(int(tr.elapsed_s * 1000)) if tr.elapsed_s else ""
        if mode == ViewMode.TASK:
            return {
                "#": "",
                "Folder": folder_cell,
                "Agent": "",
                "Model": "",
                "Tasks": "",
                "Turns": str(st.num_turns) if st and st.num_turns else "",
                "Time": elapsed,
            }
        if st is None:  # never-run task: only Folder + elapsed time
            return {c: "" for c in _METRICS_DETAIL_COLS} | {
                "#": "", "Folder": folder_cell, "Time": elapsed
            }
        return {
            "#": "",
            "Folder": folder_cell,
            "Input": _fmt_tokens(st.input_tokens),
            "Output": _fmt_tokens(st.output_tokens),
            "Avg Ctx": _fmt_tokens(st.avg_input_tokens),
            "Max Ctx": _fmt_tokens(st.max_input_tokens),
            "Cache%": f"{st.cache_hit_rate:.0f}%",
            "In/Out": _fmt_ratio(st.io_ratio),
            "LLM/Tool": _fmt_time_breakdown(st.time_breakdown),
            "TTFT": _fmt_ttft(st.ttft_ms, st.streamed),
            "Tok/s": _fmt_tok_per_sec(st.llm_tok_per_sec),
            "Time": elapsed,
        }
    # iteration row (legacy sequential folder)
    it = fs.iterations[ii]
    if mode == ViewMode.TASK:
        delta = it.tasks_completed_delta
        return {
            "#": "",
            "Folder": f"  └ {it.phase}",
            "Agent": "",
            "Model": "",
            "Tasks": str(delta) if delta else "",
            "Turns": str(it.num_turns) if it.num_turns else "",
            "Time": _fmt_time(it.wall_ms),
        }
    return {
        "#": "",
        "Folder": f"  └ {it.phase}",
        "Input": _fmt_tokens(it.input_tokens),
        "Output": _fmt_tokens(it.output_tokens),
        "Avg Ctx": _fmt_tokens(it.avg_input_tokens),
        "Max Ctx": _fmt_tokens(it.max_input_tokens),
        "Cache%": f"{it.cache_hit_rate:.0f}%",
        "In/Out": _fmt_ratio(it.io_ratio),
        "LLM/Tool": _fmt_time_breakdown(it.time_breakdown),
        "TTFT": _fmt_ttft(it.ttft_ms, it.streamed),
        "Tok/s": _fmt_tok_per_sec(it.llm_tok_per_sec),
        "Time": _fmt_time(it.wall_ms),
    }


def _build_display_rows(
    folders: list[FolderStatus], expanded: set[str]
) -> list[tuple[str, int, int]]:
    """Flatten folders + expanded sub-rows into a single ordered list.

    Each entry is (kind, folder_idx, sub_idx). For folder rows sub_idx is -1.
    Expanding a parallel-mode folder (``.ola/`` present) yields ``"task"``
    sub-rows from its per-task spine; expanding a legacy folder yields ``"iter"``
    sub-rows from its STATS.jsonl iterations. The order matches the visual order
    of the rendered table, so a flat index into this list directly addresses one
    row on screen.
    """
    rows: list[tuple[str, int, int]] = []
    for fi, fs in enumerate(folders):
        rows.append(("folder", fi, -1))
        if fs.name in expanded:
            if fs.is_parallel:
                for ti in range(len(fs.task_rows)):
                    rows.append(("task", fi, ti))
            else:
                for ii in range(len(fs.iterations)):
                    rows.append(("iter", fi, ii))
    return rows


def _folder_row_index(rows: list[tuple[str, int, int]], folder_idx: int) -> int:
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


def _append_totals_row(
    table: Table, folders: list[FolderStatus], mode: ViewMode
) -> None:
    """Append a ``TOTAL`` footer row aggregating the numeric columns.

    A grand total across *all* folders (independent of which are expanded or
    scrolled into view), drawn below a divider so it reads as a summary. Only the
    additive numeric columns are filled — tasks, turns, time, input, output, and
    context (avg = aggregate input/turn, max = the largest single call). Ratio,
    percentage, and median columns (Cache%, In/Out, LLM/Tool, TTFT, Tok/s) have
    no meaningful sum, so they are left blank rather than fabricating a number.
    No row is added for an empty agent folder.
    """
    if not folders:
        return

    completed = sum(fs.tasks_completed for fs in folders)
    total = sum(fs.tasks_total for fs in folders)
    turns = sum(fs.total_num_turns for fs in folders)
    wall_ms = sum(fs.display_wall_ms for fs in folders)
    input_tokens = sum(fs.total_input_tokens for fs in folders)
    output_tokens = sum(fs.total_output_tokens for fs in folders)
    max_ctx = max((fs.max_input_tokens for fs in folders), default=0)
    # Aggregate avg context = total input over total LLM calls (turns), matching
    # FolderStatus.avg_input_tokens but across every folder.
    avg_ctx = input_tokens // turns if turns else 0

    # Divider above the footer so it visually separates from the data rows.
    table.add_section()

    if mode == ViewMode.TASK:
        tasks_str = f"{completed}/{total}"
        if total > 0 and completed >= total:
            tasks_text = Text(tasks_str, style="green")
        elif total > 0:
            tasks_text = Text(tasks_str, style="yellow")
        else:
            tasks_text = Text(tasks_str, style="dim")
        table.add_row(
            "",
            "TOTAL",
            "",
            "",
            tasks_text,
            str(turns) if turns else "",
            _fmt_time(wall_ms),
            style="bold",
        )
    else:  # METRICS
        table.add_row(
            "",
            "TOTAL",
            _fmt_tokens(input_tokens),
            _fmt_tokens(output_tokens),
            _fmt_tokens(avg_ctx),
            _fmt_tokens(max_ctx),
            "",  # Cache% — a rate, not a sum
            "",  # In/Out — a ratio
            "",  # LLM/Tool — a split
            "",  # TTFT — a median
            "",  # Tok/s — a median
            _fmt_time(wall_ms),
            style="bold",
        )


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
    # Pin title/caption to a single line each — _TABLE_CHROME_ROWS reserves one
    # line apiece, so a wrapped title (long agent path) or caption (on a narrow
    # terminal) would push the table past the viewport budget.
    title.no_wrap = True
    title.overflow = "ellipsis"

    # Footer: keybinding hints
    caption = Text.assemble(
        ("q", "bold"),
        (": quit  ", "dim"),
        ("m", "bold"),
        (": mode  ", "dim"),
        ("\u2191\u2193", "bold"),
        (": move  ", "dim"),
        ("\u2190\u2192", "bold"),
        (": column  ", "dim"),
        ("PgUp/PgDn", "bold"),
        (": page  ", "dim"),
        ("g/G", "bold"),
        (": top/bot  ", "dim"),
        ("Enter", "bold"),
        (": expand", "dim"),
    )
    caption.no_wrap = True
    caption.overflow = "ellipsis"

    table = Table(title=title, caption=caption, expand=True, show_header=True)
    # Every column is single-line (no_wrap + ellipsis): a wrapped cell would make
    # a display row span >1 terminal line, but the viewport math in run_live
    # budgets exactly one line per display row. Folding (the rich default, and the
    # old Agent/Model setting) silently broke that invariant — a 25-folder run
    # overflowed the screen because each row folded to ~3 lines. Truncate instead.
    table.add_column("#", justify="right", style="dim", width=3, no_wrap=True)
    # Folder is the one flexible column (ratio=1): it absorbs slack space and is
    # the first to give it back, so the fixed-width numeric columns keep their
    # full content while a long folder/task label ellipsizes instead of starving
    # them.
    table.add_column(
        "Folder", style="bold", no_wrap=True, overflow="ellipsis", ratio=1
    )

    if mode == ViewMode.TASK:
        table.add_column(
            "Agent", max_width=24, no_wrap=True, overflow="ellipsis"
        )
        table.add_column(
            "Model", max_width=20, no_wrap=True, overflow="ellipsis"
        )
        table.add_column("Tasks", justify="right", no_wrap=True)
        table.add_column("Turns", justify="right", no_wrap=True)
        table.add_column("Time", justify="right", no_wrap=True)
    else:  # METRICS
        table.add_column("Input", justify="right", no_wrap=True)
        table.add_column("Output", justify="right", no_wrap=True)
        table.add_column("Avg Ctx", justify="right", no_wrap=True)
        table.add_column("Max Ctx", justify="right", no_wrap=True)
        table.add_column("Cache%", justify="right", no_wrap=True)
        table.add_column("In/Out", justify="right", no_wrap=True)
        table.add_column("LLM/Tool", justify="right", no_wrap=True)
        table.add_column("TTFT", justify="right", no_wrap=True)
        table.add_column("Tok/s", justify="right", no_wrap=True)
        table.add_column("Time", justify="right", no_wrap=True)

    for vis_idx, (kind, fi, ii) in enumerate(visible_rows):
        flat_idx = actual_offset + vis_idx
        is_cursor = cursor is not None and flat_idx == cursor
        fs = folders[fi]
        vals = _row_values(folders, kind, fi, ii, mode)

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

            # Show expand indicator when there are sub-rows (legacy iterations
            # or, in parallel mode, per-task rows).
            sub_rows = fs.task_rows if fs.is_parallel else fs.iterations
            prefix = ""
            if sub_rows:
                prefix = "\u25bc " if fs.name in expanded else "\u25b6 "

            # Active folder is distinguished by its colour (bold yellow vs
            # plain yellow for other in-progress folders), not a marker.
            folder_cell = f"{prefix}{vals['Folder']}"

            if mode == ViewMode.TASK:
                # Color the Tasks cell per completion state.
                tasks_str = vals["Tasks"]
                if fs.tasks_total > 0 and fs.tasks_completed >= fs.tasks_total:
                    tasks_text = Text(tasks_str, style="green")
                elif fs.tasks_total > 0:
                    tasks_text = Text(tasks_str, style="yellow")
                else:
                    tasks_text = Text(tasks_str, style="dim")

                table.add_row(
                    vals["#"],
                    folder_cell,
                    vals["Agent"],
                    vals["Model"],
                    tasks_text,
                    vals["Turns"],
                    vals["Time"],
                    style=style,
                )
            else:  # METRICS
                cache_text = Text(
                    vals["Cache%"], style=_cache_style(fs.cache_hit_rate)
                )

                table.add_row(
                    vals["#"],
                    folder_cell,
                    vals["Input"],
                    vals["Output"],
                    vals["Avg Ctx"],
                    vals["Max Ctx"],
                    cache_text,
                    vals["In/Out"],
                    vals["LLM/Tool"],
                    vals["TTFT"],
                    vals["Tok/s"],
                    vals["Time"],
                    style=style,
                )
        elif kind == "task":  # parallel-mode per-task row
            tr = fs.task_rows[ii]
            base_style = _task_status_style(tr.status)
            row_style = f"reverse {base_style}".strip() if is_cursor else base_style
            # The Folder cell labels each task by its PLAN.md position (1-based)
            # and its tasks.json id (so the row traces straight back into
            # tasks.json/events.jsonl), with the task text following so the row
            # stays self-describing. Status shows through the row color.
            folder_cell = vals["Folder"]
            st = tr.stats

            if mode == ViewMode.TASK:
                table.add_row(
                    "",
                    folder_cell,
                    "",
                    "",
                    "",
                    vals["Turns"],
                    vals["Time"],
                    style=row_style or None,
                )
            elif st is not None:  # METRICS — fold in the task's STATS aggregate
                cache_text = Text(
                    vals["Cache%"], style=_cache_style(st.cache_hit_rate)
                )
                table.add_row(
                    "",
                    folder_cell,
                    vals["Input"],
                    vals["Output"],
                    vals["Avg Ctx"],
                    vals["Max Ctx"],
                    cache_text,
                    vals["In/Out"],
                    vals["LLM/Tool"],
                    vals["TTFT"],
                    vals["Tok/s"],
                    vals["Time"],
                    style=row_style or None,
                )
            else:  # METRICS — a never-run task has no token metrics yet
                cells: list[Any] = ["", folder_cell] + [""] * 9 + [vals["Time"]]
                table.add_row(*cells, style=row_style or None)
        else:  # iter row
            it = fs.iterations[ii]
            iter_style = "reverse dim" if is_cursor else "dim"

            if mode == ViewMode.TASK:
                table.add_row(
                    "",
                    vals["Folder"],
                    "",
                    "",
                    vals["Tasks"],
                    vals["Turns"],
                    vals["Time"],
                    style=iter_style,
                )
            else:  # METRICS
                it_cache_text = Text(
                    vals["Cache%"],
                    style=_cache_style(it.cache_hit_rate),
                )
                table.add_row(
                    "",
                    vals["Folder"],
                    vals["Input"],
                    vals["Output"],
                    vals["Avg Ctx"],
                    vals["Max Ctx"],
                    it_cache_text,
                    vals["In/Out"],
                    vals["LLM/Tool"],
                    vals["TTFT"],
                    vals["Tok/s"],
                    vals["Time"],
                    style=iter_style,
                )

    # Grand-total footer across all folders, always pinned to the bottom (after
    # the scrolled window), so it reads as a summary regardless of offset.
    _append_totals_row(table, folders, mode)

    return table


def build_detail(
    folders: list[FolderStatus],
    expanded: set[str],
    cursor: int,
    mode: ViewMode,
    active_col: str,
    width: int,
) -> Text:
    """Build the bottom detail line: the active column's full value for the
    cursor row.

    The grid columns ellipsize, so this line is the way to read a truncated
    value in full; cycling the active column with Left/Right is the *only*
    indication of which column is active (no header is highlighted). The value
    is clipped to ``_MAX_DETAIL_LINES`` rows' worth at the current width so the
    detail can never itself overflow the viewport.
    """
    value = ""
    rows = _build_display_rows(folders, expanded)
    if rows and 0 <= cursor < len(rows):
        kind, fi, ii = rows[cursor]
        value = _row_values(folders, kind, fi, ii, mode).get(active_col, "")
    value = value.strip() or "—"  # em dash stands in for an empty cell

    # Clip so the line never exceeds _MAX_DETAIL_LINES at this width. One line is
    # held back for the label and any leading words, since a long unbroken token
    # word-wraps onto its own line before being char-broken across the rest. The
    # viewport math reserves the measured height regardless, so this only bounds
    # how much screen the detail may claim.
    budget = max(1, width) * max(1, _MAX_DETAIL_LINES - 1)
    if len(value) > budget:
        value = value[: max(1, budget - 1)] + "…"

    return Text.assemble((f"{active_col} ", "bold magenta"), (value, ""))


def build_view(
    folders: list[FolderStatus],
    expanded: set[str],
    cursor: int,
    agent_path: Path,
    mode: ViewMode,
    offset: int,
    max_rows: int,
    active_col: str,
    width: int,
) -> Group:
    """Compose the full live view: the scrolled table plus the detail line."""
    table = build_table(
        folders, expanded, cursor, agent_path, mode, offset=offset, max_rows=max_rows
    )
    detail = build_detail(folders, expanded, cursor, mode, active_col, width)
    return Group(table, detail)


def _measure_height(renderable: RenderableType, width: int) -> int:
    """Count the terminal lines a renderable occupies at the given width."""
    import io as _io

    from rich.console import Console

    console = Console(file=_io.StringIO(), width=max(1, width))
    return max(1, len(console.render_lines(renderable, console.options)))


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
    active_col = "Folder"  # column whose full value the detail line shows

    folders = read_agent_folder(agent_path)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    def view() -> Group:
        size = shutil.get_terminal_size((80, 24))
        return build_view(
            folders, expanded, cursor, agent_path, mode,
            offset, viewport_height(), active_col, size.columns,
        )

    def viewport_height() -> int:
        # The detail line lives below the table, so its (wrapped) height comes
        # out of the row budget on top of the fixed table chrome — measured each
        # tick because it depends on the active column's value at the cursor.
        size = shutil.get_terminal_size((80, 24))
        detail = build_detail(
            folders, expanded, cursor, mode, active_col, size.columns
        )
        detail_h = _measure_height(detail, size.columns)
        return max(1, size.lines - _TABLE_CHROME_ROWS - detail_h)

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
            view(),
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
                    mode = ViewMode.METRICS if mode == ViewMode.TASK else ViewMode.TASK
                    # Modes share Folder/Time but not the metric columns, so snap
                    # the active detail column back to Folder if it's gone.
                    if active_col not in _detail_cols(mode):
                        active_col = "Folder"
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
                elif key == "\x1b[D":  # Left arrow — previous detail column
                    cols = _detail_cols(mode)
                    i = cols.index(active_col) if active_col in cols else 0
                    active_col = cols[(i - 1) % len(cols)]
                    needs_update = True
                elif key == "\x1b[C":  # Right arrow — next detail column
                    cols = _detail_cols(mode)
                    i = cols.index(active_col) if active_col in cols else 0
                    active_col = cols[(i + 1) % len(cols)]
                    needs_update = True
                elif key == "\x1b[5~":  # PgUp
                    cursor = max(0, cursor - viewport_height())
                    needs_update = True
                elif key == "\x1b[6~":  # PgDn
                    rows = _build_display_rows(folders, expanded)
                    cursor = min(max(0, len(rows) - 1), cursor + viewport_height())
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
                    live.update(view(), refresh=True)

                _time.sleep(0.05)  # ~20 FPS input polling
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
