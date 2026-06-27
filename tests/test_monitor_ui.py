"""Tests for ola.monitor.ui — table building and formatting helpers."""

from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from ola.monitor.data import FolderStatus, IterationStatus, TaskRow, read_folder_status
from ola.monitor.ui import (
    _MAX_DETAIL_LINES,
    ViewMode,
    _build_display_rows,
    _cache_style,
    _detail_cols,
    _find_active_index,
    _fmt_ratio,
    _fmt_time,
    _fmt_time_breakdown,
    _fmt_tok_per_sec,
    _fmt_tokens,
    _fmt_ttft,
    _folder_row_index,
    _measure_height,
    _read_key,
    _row_values,
    _task_status_style,
    build_detail,
    build_table,
    build_view,
)


def _render_table_text(table) -> str:
    """Render a rich Table to plain text for assertion."""
    console = Console(file=StringIO(), width=120, force_terminal=True)
    console.print(table)
    return console.file.getvalue()


class TestFmtTokens:
    def test_small(self):
        assert _fmt_tokens(0) == "0"
        assert _fmt_tokens(999) == "999"

    def test_thousands(self):
        assert _fmt_tokens(1_000) == "1.0k"
        assert _fmt_tokens(45_300) == "45.3k"

    def test_millions(self):
        assert _fmt_tokens(1_000_000) == "1.0M"
        assert _fmt_tokens(1_234_567) == "1.2M"


class TestFmtTime:
    def test_seconds(self):
        assert _fmt_time(5_000) == "5s"
        assert _fmt_time(59_000) == "59s"

    def test_minutes(self):
        assert _fmt_time(60_000) == "1m00s"
        assert _fmt_time(90_000) == "1m30s"

    def test_hours(self):
        assert _fmt_time(3_600_000) == "1h00m"
        assert _fmt_time(5_430_000) == "1h30m"


class TestBuildTable:
    def test_empty(self):
        table = build_table([])
        assert table.row_count == 0

    def test_basic_rows(self):
        folders = [
            FolderStatus(
                name="task-1",
                tasks_completed=3,
                tasks_total=5,
                iterations=[
                    IterationStatus(
                        phase="task-t1-1",
                        input_tokens=10_000,
                        output_tokens=5_000,
                        cache_read_tokens=8_000,
                        wall_ms=120_000,
                    ),
                ],
            ),
            FolderStatus(
                name="task-2",
                tasks_completed=4,
                tasks_total=4,
                iterations=[
                    IterationStatus(
                        phase="task-t1-1",
                        input_tokens=20_000,
                        output_tokens=10_000,
                        cache_read_tokens=0,
                        wall_ms=60_000,
                    ),
                ],
            ),
        ]
        table = build_table(folders)
        assert table.row_count == 3  # 2 folders + TOTAL footer

    def test_dim_style_for_no_tasks(self):
        """Folders with 0 total tasks should get dim styling."""
        folders = [FolderStatus(name="empty")]
        table = build_table(folders)
        assert table.row_count == 2  # 1 folder + TOTAL footer
        # The row style should be "dim" — we check via the internal rows
        assert table.rows[0].style == "dim"

    def test_green_style_for_complete(self):
        folders = [FolderStatus(name="done", tasks_completed=3, tasks_total=3)]
        table = build_table(folders)
        assert table.rows[0].style == "green"

    def test_bold_yellow_style_for_active(self):
        """The first in-progress folder (active) gets bold yellow."""
        folders = [FolderStatus(name="wip", tasks_completed=1, tasks_total=3)]
        table = build_table(folders)
        assert table.rows[0].style == "bold yellow"

    def test_yellow_style_for_non_active_in_progress(self):
        """In-progress folders that aren't active get plain yellow."""
        folders = [
            FolderStatus(name="first", tasks_completed=1, tasks_total=3),
            FolderStatus(name="second", tasks_completed=1, tasks_total=3),
        ]
        table = build_table(folders)
        assert table.rows[0].style == "bold yellow"  # active
        assert table.rows[1].style == "yellow"  # not active

    def test_collapsed_shows_arrow(self):
        """Collapsed folders with iterations show ▶ prefix."""
        folders = [
            FolderStatus(
                name="t1",
                tasks_completed=1,
                tasks_total=2,
                iterations=[IterationStatus(phase="task-t1-1", input_tokens=100)],
            )
        ]
        table = build_table(folders, expanded=set())
        text = _render_table_text(table)
        assert "▶" in text
        assert "▼" not in text
        # No sub-rows (folder row only) + TOTAL footer
        assert table.row_count == 2

    def test_expanded_shows_iterations(self):
        """Expanded folders render iteration sub-rows."""
        iters = [
            IterationStatus(
                phase="task-t1-1",
                input_tokens=10_000,
                output_tokens=5_000,
                cache_read_tokens=8_000,
                wall_ms=60_000,
            ),
            IterationStatus(
                phase="loop-1",
                input_tokens=20_000,
                output_tokens=10_000,
                cache_read_tokens=15_000,
                wall_ms=90_000,
            ),
        ]
        folders = [
            FolderStatus(
                name="t1",
                tasks_completed=2,
                tasks_total=3,
                iterations=iters,
            )
        ]
        table = build_table(folders, expanded={"t1"})
        # 1 parent + 2 iteration rows + TOTAL footer
        assert table.row_count == 4
        text = _render_table_text(table)
        assert "▼" in text
        assert "task-t1-1" in text
        assert "loop-1" in text

    def test_expanded_no_iterations(self):
        """Expanding a folder with no iterations adds no sub-rows."""
        folders = [FolderStatus(name="empty")]
        table = build_table(folders, expanded={"empty"})
        assert table.row_count == 2  # folder (no iterations) + TOTAL footer

    def test_mixed_expanded_collapsed(self):
        """Only expanded folders get sub-rows."""
        folders = [
            FolderStatus(
                name="a",
                iterations=[IterationStatus(phase="task-t1-1")],
            ),
            FolderStatus(
                name="b",
                iterations=[
                    IterationStatus(phase="task-t1-1"),
                    IterationStatus(phase="loop-1"),
                ],
            ),
        ]
        table = build_table(folders, expanded={"b"})
        # a: 1 row, b: 1 parent + 2 iterations = 4, + TOTAL footer
        assert table.row_count == 5

    def test_cursor_highlights_row(self):
        """The cursor row should use reverse styling."""
        folders = [
            FolderStatus(name="a", tasks_completed=1, tasks_total=2),
            FolderStatus(name="b", tasks_completed=2, tasks_total=2),
        ]
        table = build_table(folders, cursor=0)
        # Row 0 has cursor (reverse yellow), row 1 does not
        assert "reverse" in (table.rows[0].style or "")
        assert "reverse" not in (table.rows[1].style or "")

    def test_cursor_on_second_row(self):
        folders = [
            FolderStatus(name="a", tasks_completed=1, tasks_total=2),
            FolderStatus(name="b", tasks_completed=2, tasks_total=2),
        ]
        table = build_table(folders, cursor=1)
        assert "reverse" not in (table.rows[0].style or "")
        assert "reverse" in (table.rows[1].style or "")

    def test_number_column_present_task_mode(self):
        """Task mode: 7 columns — #, Folder, Agent, Model, Tasks, Turns, Time."""
        folders = [
            FolderStatus(name="a"),
            FolderStatus(name="b"),
        ]
        table = build_table(folders, mode=ViewMode.TASK)
        assert len(table.columns) == 7
        assert table.columns[0].header == "#"

    def test_number_column_present_metrics_mode(self):
        """Metrics mode: 12 columns — #, Folder, Input, Output, Avg Ctx, Max Ctx, Cache%, In/Out, LLM/Tool, TTFT, Tok/s, Time."""
        folders = [
            FolderStatus(name="a"),
            FolderStatus(name="b"),
        ]
        table = build_table(folders, mode=ViewMode.METRICS)
        assert len(table.columns) == 12
        assert table.columns[0].header == "#"


class TestTotalsRow:
    """The grand-total footer row pinned to the bottom of the table."""

    def _two_folders(self) -> list[FolderStatus]:
        return [
            FolderStatus(
                name="01-a",
                tasks_completed=3,
                tasks_total=5,
                iterations=[
                    IterationStatus(
                        phase="t1",
                        input_tokens=10_000,
                        output_tokens=5_000,
                        num_turns=4,
                        wall_ms=120_000,
                        max_input_tokens=8_000,
                    )
                ],
            ),
            FolderStatus(
                name="02-b",
                tasks_completed=4,
                tasks_total=4,
                iterations=[
                    IterationStatus(
                        phase="t1",
                        input_tokens=20_000,
                        output_tokens=10_000,
                        num_turns=6,
                        wall_ms=60_000,
                        max_input_tokens=15_000,
                    )
                ],
            ),
        ]

    def test_no_totals_for_empty_agent_folder(self):
        """An empty agent folder gets no footer (and no divider)."""
        assert build_table([]).row_count == 0

    def _total_line(self, table) -> str:
        lines = [ln for ln in _render_table_text(table).splitlines() if "TOTAL" in ln]
        assert lines, "no TOTAL row rendered"
        return lines[0]

    def test_task_view_sums_tasks_turns_time(self):
        table = build_table(self._two_folders(), mode=ViewMode.TASK)
        # The footer is the last row, rendered bold.
        assert table.rows[-1].style == "bold"
        line = self._total_line(table)
        assert "7/9" in line  # completed 3+4 / total 5+4, summed separately
        assert "10" in line  # turns 4 + 6
        assert "3m00s" in line  # wall 2m + 1m

    def test_metrics_view_sums_tokens_and_ctx_blanks_ratios(self):
        table = build_table(self._two_folders(), mode=ViewMode.METRICS)
        line = self._total_line(table)
        assert "30.0k" in line  # input 10k + 20k
        assert "15.0k" in line  # output 5k + 10k (and max ctx = max(8k, 15k))
        assert "3.0k" in line  # avg ctx = total input 30k // total turns 10
        # Ratio / percentage / median columns have no meaningful total: blank.
        assert "%" not in line  # no Cache% / LLM-Tool split
        assert "x" not in line  # no In/Out ratio

    def test_totals_pinned_below_a_scrolled_window(self):
        """The footer is appended after the window, whatever the scroll offset."""
        folders = [
            FolderStatus(
                name="big",
                tasks_completed=1,
                tasks_total=2,
                iterations=[
                    IterationStatus(phase=f"loop-{i}", wall_ms=1000)
                    for i in range(20)
                ],
            )
        ]
        table = build_table(folders, expanded={"big"}, offset=10, max_rows=5)
        assert table.rows[-1].style == "bold"
        assert "TOTAL" in _render_table_text(table)


class TestHeaderFooter:
    def test_header_shows_path(self):
        """Header should include the agent path."""
        folders = [FolderStatus(name="t1")]
        table = build_table(folders, agent_path=Path("/tmp/agent"))
        text = _render_table_text(table)
        assert "ola-top" in text
        assert "/tmp/agent" in text

    def test_footer_shows_keybindings(self):
        """Footer should include keybinding hints."""
        folders = [FolderStatus(name="t1")]
        table = build_table(folders, agent_path=Path("/tmp/agent"))
        text = _render_table_text(table)
        assert "quit" in text
        assert "move" in text
        assert "page" in text
        assert "expand" in text

    def test_header_without_path(self):
        """Header should work when no agent_path is provided."""
        folders = [FolderStatus(name="t1")]
        table = build_table(folders)
        text = _render_table_text(table)
        assert "ola-top" in text


class TestCacheStyle:
    def test_high_cache(self):
        assert _cache_style(80.0) == "green"
        assert _cache_style(50.0) == "green"

    def test_medium_cache(self):
        assert _cache_style(30.0) == "yellow"
        assert _cache_style(25.0) == "yellow"

    def test_low_cache(self):
        assert _cache_style(10.0) == "red"
        assert _cache_style(0.0) == "red"


class TestFindActiveIndex:
    def test_no_folders(self):
        assert _find_active_index([]) is None

    def test_all_complete(self):
        folders = [FolderStatus(name="a", tasks_completed=3, tasks_total=3)]
        assert _find_active_index(folders) is None

    def test_first_incomplete(self):
        folders = [
            FolderStatus(name="a", tasks_completed=3, tasks_total=3),
            FolderStatus(name="b", tasks_completed=1, tasks_total=3),
            FolderStatus(name="c", tasks_completed=0, tasks_total=2),
        ]
        assert _find_active_index(folders) == 1

    def test_no_tasks(self):
        folders = [FolderStatus(name="a")]
        assert _find_active_index(folders) is None


class TestActiveColour:
    def test_active_folder_is_bold_yellow(self):
        """The active folder reads off colour (bold yellow), not a marker."""
        folders = [
            FolderStatus(name="done", tasks_completed=3, tasks_total=3),
            FolderStatus(name="active", tasks_completed=1, tasks_total=3),
        ]
        table = build_table(folders)
        text = _render_table_text(table)
        # No marker is rendered any more — status reads off colour alone.
        assert "\u25cf" not in text
        assert table.rows[1].style == "bold yellow"
        assert table.rows[0].style == "green"

    def test_inactive_in_progress_is_plain_yellow(self):
        """A later in-progress folder is plain yellow, distinct from active."""
        folders = [
            FolderStatus(name="active", tasks_completed=1, tasks_total=3),
            FolderStatus(name="later", tasks_completed=0, tasks_total=2),
        ]
        table = build_table(folders)
        assert table.rows[0].style == "bold yellow"
        assert table.rows[1].style == "yellow"

    def test_no_marker_when_all_complete(self):
        """No marker when all folders are complete."""
        folders = [
            FolderStatus(name="a", tasks_completed=3, tasks_total=3),
            FolderStatus(name="b", tasks_completed=2, tasks_total=2),
        ]
        table = build_table(folders)
        text = _render_table_text(table)
        assert "\u25cf" not in text


class TestReadKey:
    def test_no_key_ready(self):
        """Returns None when no input is available."""
        with patch("ola.monitor.ui.select") as mock_select:
            mock_select.select.return_value = ([], [], [])
            assert _read_key(0) is None

    def test_regular_key(self):
        """Returns a single character for a regular keypress."""
        with (
            patch("ola.monitor.ui.select") as mock_select,
            patch("ola.monitor.ui.os") as mock_os,
        ):
            mock_select.select.return_value = ([0], [], [])
            mock_os.read.return_value = b"q"
            assert _read_key(0) == "q"

    def test_arrow_key_up(self):
        """Returns the full escape sequence for arrow keys."""
        with (
            patch("ola.monitor.ui.select") as mock_select,
            patch("ola.monitor.ui.os") as mock_os,
        ):
            mock_select.select.side_effect = [
                ([0], [], []),  # initial: key ready
                ([0], [], []),  # 0.1s wait: rest of sequence available
            ]
            mock_os.read.side_effect = [b"\x1b", b"[A"]
            assert _read_key(0) == "\x1b[A"

    def test_bare_escape(self):
        """Returns bare ESC when no follow-up bytes arrive."""
        with (
            patch("ola.monitor.ui.select") as mock_select,
            patch("ola.monitor.ui.os") as mock_os,
        ):
            mock_select.select.side_effect = [
                ([0], [], []),  # initial: key ready
                ([], [], []),  # 0.1s wait: nothing follows
            ]
            mock_os.read.return_value = b"\x1b"
            assert _read_key(0) == "\x1b"


class TestFmtRatio:
    def test_zero(self):
        assert _fmt_ratio(0.0) == "-"

    def test_normal(self):
        assert _fmt_ratio(4.2) == "4.2x"

    def test_large(self):
        assert _fmt_ratio(150.0) == "150x"


class TestFmtTokPerSec:
    def test_zero(self):
        assert _fmt_tok_per_sec(0.0) == "-"

    def test_small(self):
        assert _fmt_tok_per_sec(42.5) == "42.5"

    def test_large(self):
        assert _fmt_tok_per_sec(150.3) == "150"


class TestFmtTTFT:
    def test_zero(self):
        assert _fmt_ttft(0) == "-"

    def test_milliseconds(self):
        assert _fmt_ttft(500) == "500ms"

    def test_one_ms(self):
        assert _fmt_ttft(1) == "1ms"

    def test_boundary(self):
        assert _fmt_ttft(999) == "999ms"

    def test_seconds(self):
        assert _fmt_ttft(1500) == "1.5s"

    def test_large(self):
        assert _fmt_ttft(12345) == "12.3s"


class TestFmtTimeBreakdown:
    def test_normal(self):
        assert _fmt_time_breakdown((70.0, 25.0)) == "70/25%"

    def test_all_llm(self):
        assert _fmt_time_breakdown((100.0, 0.0)) == "100/0%"


class TestBuildDisplayRows:
    def test_no_expanded(self):
        folders = [
            FolderStatus(name="a", iterations=[IterationStatus(phase="task-t1-1")]),
            FolderStatus(name="b", iterations=[IterationStatus(phase="task-t1-1")]),
        ]
        rows = _build_display_rows(folders, set())
        assert rows == [("folder", 0, -1), ("folder", 1, -1)]

    def test_one_expanded(self):
        folders = [
            FolderStatus(
                name="a",
                iterations=[
                    IterationStatus(phase="task-t1-1"),
                    IterationStatus(phase="loop-1"),
                ],
            ),
            FolderStatus(name="b", iterations=[IterationStatus(phase="task-t1-1")]),
        ]
        rows = _build_display_rows(folders, {"a"})
        assert rows == [
            ("folder", 0, -1),
            ("iter", 0, 0),
            ("iter", 0, 1),
            ("folder", 1, -1),
        ]

    def test_folder_row_index(self):
        rows = [
            ("folder", 0, -1),
            ("iter", 0, 0),
            ("iter", 0, 1),
            ("folder", 1, -1),
            ("folder", 2, -1),
        ]
        assert _folder_row_index(rows, 0) == 0
        assert _folder_row_index(rows, 1) == 3
        assert _folder_row_index(rows, 2) == 4
        # Missing folder falls back to 0
        assert _folder_row_index(rows, 99) == 0


class TestViewport:
    def _big_folder(self, n_iters):
        return [
            FolderStatus(
                name="big",
                tasks_completed=1,
                tasks_total=2,
                iterations=[
                    IterationStatus(phase=f"loop-{i}", wall_ms=1000)
                    for i in range(n_iters)
                ],
            )
        ]

    def test_max_rows_truncates(self):
        """A window smaller than the total clamps the row count."""
        folders = self._big_folder(20)
        # 1 folder + 20 iters = 21 display rows
        table = build_table(folders, expanded={"big"}, max_rows=5)
        # 5 windowed data rows + TOTAL footer (the footer is always appended,
        # below the scrolled window, and is not capped by max_rows).
        assert table.row_count == 6

    def test_offset_skips_rows(self):
        """offset advances the window into the iteration list."""
        folders = self._big_folder(20)
        table = build_table(folders, expanded={"big"}, offset=10, max_rows=5)
        assert table.row_count == 6  # 5 windowed rows + TOTAL footer
        text = _render_table_text(table)
        # display_rows[10:15] = iters 9..13
        assert "loop-9" in text
        assert "loop-13" in text
        # rows outside the window are absent
        assert "loop-0 " not in text
        assert "loop-19" not in text

    def test_offset_clamped_to_end(self):
        """An out-of-range offset clamps to the last full window."""
        folders = self._big_folder(20)
        table = build_table(folders, expanded={"big"}, offset=999, max_rows=5)
        assert table.row_count == 6  # 5 windowed rows + TOTAL footer
        text = _render_table_text(table)
        # Window snaps to display_rows[16:21] = iters 15..19
        assert "loop-19" in text

    def test_max_rows_none_renders_all(self):
        folders = self._big_folder(20)
        table = build_table(folders, expanded={"big"}, max_rows=None)
        assert table.row_count == 22  # 21 display rows + TOTAL footer

    def test_cursor_on_iteration_row(self):
        """Cursor highlight follows the flat row index, including iter rows."""
        folders = self._big_folder(5)
        # Flat rows: 0=folder, 1=iter0, 2=iter1, ...
        table = build_table(folders, expanded={"big"}, cursor=2)
        assert "reverse" not in (table.rows[0].style or "")
        assert "reverse" not in (table.rows[1].style or "")
        assert "reverse" in (table.rows[2].style or "")

    def test_cursor_visible_in_window(self):
        """Cursor outside the rendered window does not crash and the window is honored."""
        folders = self._big_folder(20)
        # Cursor at row 15 but window is rows 0..4 — build_table doesn't
        # adjust offset itself, that's run_live's job. Just verify it renders.
        table = build_table(folders, expanded={"big"}, cursor=15, offset=0, max_rows=5)
        assert table.row_count == 6  # 5 windowed rows + TOTAL footer

    def test_indicator_in_title(self):
        folders = self._big_folder(20)
        table = build_table(folders, expanded={"big"}, cursor=3, max_rows=5)
        text = _render_table_text(table)
        # cursor 3 of 21 rows (1-indexed display)
        assert "4/21" in text


class TestMetricsMode:
    def test_metrics_mode_renders_token_columns(self):
        """Metrics mode should show Input, Output, Cache%, In/Out columns."""
        folders = [
            FolderStatus(
                name="t1",
                tasks_completed=2,
                tasks_total=3,
                iterations=[
                    IterationStatus(
                        phase="task-t1-1",
                        input_tokens=10_000,
                        output_tokens=2_000,
                        cache_read_tokens=8_000,
                        wall_ms=60_000,
                        tool_ms=20_000,
                    ),
                ],
            )
        ]
        table = build_table(folders, mode=ViewMode.METRICS)
        text = _render_table_text(table)
        assert "10.0k" in text  # input
        assert "2.0k" in text  # output
        assert "5.0x" in text  # in/out ratio

    def test_metrics_mode_expanded_shows_breakdown(self):
        """Expanded rows in metrics mode show per-iteration metrics."""
        iters = [
            IterationStatus(
                phase="task-t1-1",
                input_tokens=10_000,
                output_tokens=5_000,
                cache_read_tokens=8_000,
                wall_ms=60_000,
                tool_ms=20_000,
            ),
        ]
        folders = [
            FolderStatus(name="t1", tasks_completed=1, tasks_total=2, iterations=iters)
        ]
        table = build_table(folders, expanded={"t1"}, mode=ViewMode.METRICS)
        assert table.row_count == 3  # folder + 1 iteration + TOTAL footer
        text = _render_table_text(table)
        assert "task-t1-1" in text

    def test_task_mode_no_token_columns(self):
        """Task mode should not show Input/Output/Cache% columns."""
        folders = [
            FolderStatus(
                name="t1",
                tasks_completed=2,
                tasks_total=3,
                iterations=[
                    IterationStatus(
                        phase="task-t1-1",
                        input_tokens=10_000,
                        output_tokens=5_000,
                        wall_ms=60_000,
                    ),
                ],
            )
        ]
        table = build_table(folders, mode=ViewMode.TASK)
        # Task mode has 7 columns, no Input/Output/Cache%
        assert len(table.columns) == 7
        headers = [c.header for c in table.columns]
        assert "Input" not in headers
        assert "Output" not in headers
        assert "Cache%" not in headers

    def test_task_mode_expanded_shows_delta(self):
        """Expanded rows in task mode show tasks_completed_delta."""
        iters = [
            IterationStatus(
                phase="task-t1-1",
                wall_ms=60_000,
                tasks_completed_delta=2,
            ),
            IterationStatus(
                phase="loop-1",
                wall_ms=30_000,
                tasks_completed_delta=1,
            ),
        ]
        folders = [
            FolderStatus(name="t1", tasks_completed=3, tasks_total=5, iterations=iters)
        ]
        table = build_table(folders, expanded={"t1"}, mode=ViewMode.TASK)
        assert table.row_count == 4  # folder + 2 iterations + TOTAL footer

    def test_header_shows_mode(self):
        """Header should display the current mode label."""
        folders = [FolderStatus(name="t1")]
        table = build_table(folders, mode=ViewMode.TASK)
        text = _render_table_text(table)
        assert "TASK" in text

        table = build_table(folders, mode=ViewMode.METRICS)
        text = _render_table_text(table)
        assert "METRICS" in text

    def test_footer_shows_mode_hint(self):
        """Footer should include 'm: mode' keybinding hint."""
        folders = [FolderStatus(name="t1")]
        table = build_table(folders)
        text = _render_table_text(table)
        assert "mode" in text


class TestTaskStatusStyle:
    def test_known_statuses(self):
        assert _task_status_style("complete") == "green"
        assert _task_status_style("running") == "cyan"
        assert _task_status_style("failed") == "red"
        assert _task_status_style("pending") == "dim"

    def test_unknown_status(self):
        assert _task_status_style("weird") == ""


def _parallel_folder(**overrides) -> FolderStatus:
    """A parallel-mode FolderStatus with a small per-task spine."""
    defaults = dict(
        name="09-parallel",
        tasks_completed=1,
        tasks_total=3,
        concurrency_cap=3,
        task_rows=[
            TaskRow(
                task_id="t-aaa",
                text="Refactor extractor",
                status="complete",
                attempt=1,
                elapsed_s=42.0,
                last_progress_message="ticked checkbox",
            ),
            TaskRow(
                task_id="t-bbb",
                text="Add HTTP client",
                status="running",
                attempt=0,
                elapsed_s=12.0,
                last_progress_message="running tests",
            ),
            TaskRow(
                task_id="t-ccc",
                text="Write docs",
                status="pending",
                attempt=0,
            ),
        ],
    )
    defaults.update(overrides)
    return FolderStatus(**defaults)


class TestParallelTaskView:
    def test_collapsed_shows_arrow_no_badge(self):
        """A parallel folder shows the expand arrow but no clever badge."""
        folders = [_parallel_folder()]
        table = build_table(folders, expanded=set())
        # Folder row only (+ TOTAL footer); task rows hidden until expanded.
        assert table.row_count == 2
        text = _render_table_text(table)
        assert "▶" in text
        # No running/cap enrichment crammed into the folder cell.
        assert "cap" not in text
        assert "running" not in text

    def test_expanded_renders_task_rows(self):
        """Expanding a parallel folder renders one sub-row per task.

        Each sub-row is labelled ``Task <pos> (<task_id>)`` (position is the
        1-based PLAN.md order, the id traces back into tasks.json /
        events.jsonl), with the task text following and elapsed time in the
        Time column. Status is conveyed by row color, not text.
        """
        folders = [_parallel_folder()]
        table = build_table(folders, expanded={"09-parallel"})
        # 1 folder + 3 task rows + TOTAL footer.
        assert table.row_count == 5
        text = _render_table_text(table)
        assert "▼" in text
        assert "Refactor extractor" in text
        assert "Add HTTP client" in text
        assert "Write docs" in text
        # Task ids surface for file traceability.
        assert "t-aaa" in text
        assert "t-bbb" in text
        # The Task #/id label is present.
        assert "Task 1 (t-aaa)" in text
        assert "Task 2 (t-bbb)" in text
        # Elapsed time surfaces in the Time column.
        assert "42s" in text
        # The live progress message is no longer crammed into the Model column.
        assert "running tests" not in text

    def test_task_row_shows_turns_in_task_view(self):
        """A task's STATS aggregate surfaces its turn count in TASK view."""
        folder = _parallel_folder()
        folder.task_rows[0].stats = IterationStatus(phase="", num_turns=7)
        table = build_table([folder], expanded={"09-parallel"})
        assert "7" in _render_table_text(table)

    def test_task_row_shows_metrics_in_metrics_view(self):
        """A task's STATS aggregate surfaces tokens/turns in METRICS view."""
        folder = _parallel_folder()
        folder.task_rows[0].stats = IterationStatus(
            phase="", input_tokens=12000, output_tokens=3000, num_turns=4
        )
        table = build_table([folder], expanded={"09-parallel"}, mode=ViewMode.METRICS)
        text = _render_table_text(table)
        assert "12.0k" in text  # input tokens
        assert "3.0k" in text  # output tokens

    def test_task_row_without_stats_leaves_metrics_blank(self):
        """A never-run task (stats=None) renders no synthetic zeros."""
        folder = _parallel_folder()  # task_rows carry no stats
        table = build_table([folder], expanded={"09-parallel"}, mode=ViewMode.METRICS)
        # Folder row still renders; task rows show only elapsed, no token cells.
        assert table.row_count == 5  # folder + 3 task rows + TOTAL footer

    def test_build_display_rows_uses_tasks_for_parallel(self):
        """A parallel folder's expanded sub-rows are 'task', not 'iter'."""
        folders = [_parallel_folder()]
        rows = _build_display_rows(folders, {"09-parallel"})
        assert rows == [
            ("folder", 0, -1),
            ("task", 0, 0),
            ("task", 0, 1),
            ("task", 0, 2),
        ]

    def test_task_row_status_colors(self):
        """Each task sub-row is colored by its status."""
        folders = [_parallel_folder()]
        table = build_table(folders, expanded={"09-parallel"})
        # rows[0] is the folder; 1..3 are tasks in spine order.
        assert table.rows[1].style == "green"  # complete
        assert table.rows[2].style == "cyan"  # running
        assert table.rows[3].style == "dim"  # pending

    def test_cursor_on_task_row(self):
        """The cursor highlight follows the flat index onto task rows."""
        folders = [_parallel_folder()]
        table = build_table(folders, expanded={"09-parallel"}, cursor=2)
        assert "reverse" in (table.rows[2].style or "")
        assert "reverse" not in (table.rows[1].style or "")

    def test_paused_folder_renders_without_badge(self):
        """A paused folder (cap 0) renders its row without any cap/running badge."""
        folders = [_parallel_folder(concurrency_cap=0)]
        text = _render_table_text(build_table(folders))
        assert "cap" not in text
        assert "running" not in text

    def test_non_parallel_folder_unchanged(self):
        """A folder without .ola/ (concurrency_cap None) shows no badge."""
        folders = [
            FolderStatus(
                name="01-legacy",
                tasks_completed=1,
                tasks_total=2,
                iterations=[IterationStatus(phase="task-t1-1", wall_ms=1000)],
            )
        ]
        text = _render_table_text(build_table(folders, expanded={"01-legacy"}))
        assert "cap" not in text
        # Legacy folders still expand to iteration rows.
        assert "task-t1-1" in text

    def test_metrics_mode_renders_task_rows(self):
        """Task rows render in METRICS mode too (with empty metric cells)."""
        folders = [_parallel_folder()]
        table = build_table(folders, expanded={"09-parallel"}, mode=ViewMode.METRICS)
        # 1 folder + 3 task rows + TOTAL footer; metric cells stay empty but
        # rows still render.
        assert table.row_count == 5
        assert table.rows[1].style == "green"  # complete task colored by status


def test_read_folder_status_populates_parallel_view(tmp_path):
    """End-to-end: a folder with .ola/ yields a parallel FolderStatus snapshot."""
    folder = tmp_path / "09-parallel"
    folder.mkdir()
    folder.joinpath("PLAN.md").write_text("- [x] One\n- [ ] Two\n")
    ola = folder / ".ola"
    ola.mkdir()
    ola.joinpath("concurrency").write_text("2\n")
    ola.joinpath("tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "t-aaa",
                        "text": "One",
                        "line_no": 1,
                        "status": "complete",
                        "attempts": 1,
                    },
                    {
                        "task_id": "t-bbb",
                        "text": "Two",
                        "line_no": 2,
                        "status": "running",
                        "attempts": 0,
                    },
                ]
            }
        )
    )

    status = read_folder_status(folder)
    assert status.is_parallel
    assert status.concurrency_cap == 2
    assert status.running_count == 1
    assert [r.task_id for r in status.task_rows] == ["t-aaa", "t-bbb"]

    text = _render_table_text(build_table([status], expanded={"09-parallel"}))
    assert "cap" not in text  # no running/cap badge
    assert "One" in text
    assert "Two" in text
    assert "t-aaa" in text  # task ids trace back into tasks.json


def _render(renderable, width: int = 120) -> str:
    """Render any rich renderable to plain text for assertion."""
    console = Console(file=StringIO(), width=width, force_terminal=True)
    console.print(renderable)
    return console.file.getvalue()


class TestDetailLine:
    """The bottom detail line: the active column's full value for the cursor
    row, cycled with Left/Right. The only indication of the active column."""

    def test_detail_cols_start_with_folder_in_both_modes(self):
        assert _detail_cols(ViewMode.TASK)[0] == "Folder"
        assert _detail_cols(ViewMode.METRICS)[0] == "Folder"
        assert "Agent" in _detail_cols(ViewMode.TASK)
        assert "Cache%" in _detail_cols(ViewMode.METRICS)
        # "#" is never cycle-able — it is just the row number.
        assert "#" not in _detail_cols(ViewMode.TASK)
        assert "#" not in _detail_cols(ViewMode.METRICS)

    def test_default_folder_shows_folder_name(self):
        folders = [FolderStatus(name="09-parallel", tasks_completed=1, tasks_total=3)]
        detail = build_detail(folders, set(), 0, ViewMode.TASK, "Folder", 120)
        assert "Folder" in detail.plain
        assert "09-parallel" in detail.plain

    def test_shows_full_agent_string_untruncated(self):
        """The Agent value is shown in full even though the grid column (24 wide)
        ellipsizes it."""
        long_agent = "Claude Code 2.1.177 (Claude Code)"
        folders = [
            FolderStatus(
                name="01-foo",
                tasks_completed=1,
                tasks_total=1,
                iterations=[
                    IterationStatus(
                        phase="loop-1", agent="cc", agent_version="2.1.177 (Claude Code)"
                    )
                ],
            )
        ]
        # Grid truncates.
        grid = _render_table_text(build_table(folders, mode=ViewMode.TASK))
        assert long_agent not in grid
        # Detail shows it whole.
        detail = build_detail(folders, set(), 0, ViewMode.TASK, "Agent", 120)
        assert long_agent in detail.plain

    def test_shows_full_task_text_untruncated(self):
        long_text = "Rename project " + "x" * 80
        folder = _parallel_folder()
        folder.task_rows[0].text = long_text
        rows = _build_display_rows([folder], {"09-parallel"})
        task_cursor = next(i for i, r in enumerate(rows) if r[0] == "task")
        detail = build_detail(
            [folder], {"09-parallel"}, task_cursor, ViewMode.TASK, "Folder", 200
        )
        assert long_text in detail.plain

    def test_empty_cell_shows_dash(self):
        """A column with no value for the cursor row reads as an em dash."""
        folders = [FolderStatus(name="01-foo", tasks_completed=0, tasks_total=1)]
        detail = build_detail(folders, set(), 0, ViewMode.TASK, "Turns", 120)
        assert "—" in detail.plain

    def test_long_value_clipped_to_max_lines(self):
        """A value longer than the detail budget is clipped so the line can never
        exceed _MAX_DETAIL_LINES — the viewport invariant must hold."""
        folder = _parallel_folder()
        folder.task_rows[0].text = "y" * 5000
        rows = _build_display_rows([folder], {"09-parallel"})
        task_cursor = next(i for i, r in enumerate(rows) if r[0] == "task")
        width = 80
        detail = build_detail(
            [folder], {"09-parallel"}, task_cursor, ViewMode.TASK, "Folder", width
        )
        assert detail.plain.endswith("…")
        assert _measure_height(detail, width) <= _MAX_DETAIL_LINES

    def test_build_view_groups_table_and_detail(self):
        folders = [FolderStatus(name="09-parallel", tasks_completed=1, tasks_total=3)]
        view = build_view(
            folders, set(), 0, Path("/agent"), ViewMode.TASK, 0, 20, "Folder", 120
        )
        text = re.sub(r"\x1b\[[0-9;]*m", "", _render(view))
        assert "ola-top" in text  # the table
        assert "09-parallel" in text  # both grid and detail
        # The detail line's label is present on the last line, below the table.
        assert text.rstrip().splitlines()[-1].lstrip().startswith("Folder")

    def test_detail_matches_row_values(self):
        """build_detail reads the same value source as the grid (_row_values)."""
        folder = _parallel_folder()
        vals = _row_values([folder], "folder", 0, -1, ViewMode.TASK)
        detail = build_detail([folder], set(), 0, ViewMode.TASK, "Tasks", 120)
        assert vals["Tasks"] in detail.plain
