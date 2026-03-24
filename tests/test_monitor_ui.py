"""Tests for ola.monitor.ui — table building and formatting helpers."""

from __future__ import annotations

from ola.monitor.data import FolderStatus, IterationStatus
from ola.monitor.ui import _fmt_time, _fmt_tokens, build_table


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
                        phase="seed",
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
                        phase="seed",
                        input_tokens=20_000,
                        output_tokens=10_000,
                        cache_read_tokens=0,
                        wall_ms=60_000,
                    ),
                ],
            ),
        ]
        table = build_table(folders)
        assert table.row_count == 2

    def test_dim_style_for_no_tasks(self):
        """Folders with 0 total tasks should get dim styling."""
        folders = [FolderStatus(name="empty")]
        table = build_table(folders)
        assert table.row_count == 1
        # The row style should be "dim" — we check via the internal rows
        assert table.rows[0].style == "dim"

    def test_green_style_for_complete(self):
        folders = [FolderStatus(name="done", tasks_completed=3, tasks_total=3)]
        table = build_table(folders)
        assert table.rows[0].style == "green"

    def test_yellow_style_for_in_progress(self):
        folders = [FolderStatus(name="wip", tasks_completed=1, tasks_total=3)]
        table = build_table(folders)
        assert table.rows[0].style == "yellow"
