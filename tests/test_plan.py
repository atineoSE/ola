"""Regression tests for ola.plan — checkbox parser + Path-based wrappers."""

from pathlib import Path

from ola.plan import count_tasks, has_outstanding_tasks, parse_task_counts


class TestParseTaskCounts:
    """Tests for the canonical parse_task_counts string parser."""

    def test_fenced_bash_block_ignored(self):
        """Regression sentinel: checkbox inside ```bash block must not count."""
        text = (
            "- [x] Real completed task\n"
            "\n"
            "```bash\n"
            'echo "- [ ] Print hello" > /tmp/PLAN.md\n'
            "```\n"
        )
        assert parse_task_counts(text) == (1, 1)

    def test_fenced_tilde_block_ignored(self):
        text = (
            "- [ ] Real unchecked\n"
            "\n"
            "~~~\n"
            "- [ ] fake inside tilde fence\n"
            "~~~\n"
        )
        assert parse_task_counts(text) == (0, 1)

    def test_indented_subtasks_counted(self):
        text = (
            "- [ ] Parent task\n"
            "  - [ ] Indented subtask\n"
            "\t- [x] Tab-indented done\n"
        )
        assert parse_task_counts(text) == (1, 3)

    def test_asterisk_and_plus_markers(self):
        text = "* [ ] asterisk\n+ [x] plus\n"
        assert parse_task_counts(text) == (1, 2)

    def test_inline_backtick_false_positive(self):
        """Prose line with checkbox inside backticks is NOT a real checkbox.

        The checkbox regex requires line-start anchoring, so inline occurrences
        like ``See `- [ ] example` below`` don't match.
        """
        text = "See `- [ ] example` below for the syntax.\n"
        assert parse_task_counts(text) == (0, 0)

    def test_mixed_case_x(self):
        text = "- [X] Done with uppercase\n- [x] Done with lowercase\n"
        assert parse_task_counts(text) == (2, 2)

    def test_trailing_space_required(self):
        """'- [ ]notspace' should NOT be counted as a checkbox."""
        text = "- [ ]notspace\n- [ ] real task\n"
        assert parse_task_counts(text) == (0, 1)

    def test_empty_text(self):
        assert parse_task_counts("") == (0, 0)

    def test_no_checkboxes(self):
        assert parse_task_counts("# Just a heading\nSome prose.\n") == (0, 0)

    def test_real_failing_plan_md(self):
        """Lock in the exact bug: agent/01-fix-stats/PLAN.md has 26 real [x]
        items and 1 [x] inside a fenced code block that the old regex counted."""
        plan_path = (
            Path(__file__).resolve().parent.parent.parent
            / "agent"
            / "01-fix-stats"
            / "PLAN.md"
        )
        if not plan_path.exists():
            import pytest

            pytest.skip("agent/01-fix-stats/PLAN.md not found in repo")
        text = plan_path.read_text()
        assert parse_task_counts(text) == (26, 26)


class TestCountTasks:
    def test_missing_file(self, tmp_path):
        assert count_tasks(tmp_path) == (0, 0)

    def test_basic(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [x] done\n- [ ] todo\n")
        assert count_tasks(tmp_path) == (1, 2)


class TestHasOutstandingTasks:
    def test_no_plan_file(self, tmp_path):
        assert has_outstanding_tasks(tmp_path) is False

    def test_all_complete(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [x] done\n- [x] also done\n")
        assert has_outstanding_tasks(tmp_path) is False

    def test_has_unchecked(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [x] done\n- [ ] todo\n")
        assert has_outstanding_tasks(tmp_path) is True

    def test_fenced_block_not_outstanding(self, tmp_path):
        """Regression: checkbox inside code block must not make tasks outstanding."""
        text = (
            "- [x] Real task done\n"
            "\n"
            "```bash\n"
            'echo "- [ ] Fake" > plan.md\n'
            "```\n"
        )
        (tmp_path / "PLAN.md").write_text(text)
        assert has_outstanding_tasks(tmp_path) is False
