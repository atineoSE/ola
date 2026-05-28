"""Regression tests for ola.plan — checkbox parser + Path-based wrappers."""

import hashlib
from pathlib import Path

from ola.plan import (
    Task,
    count_tasks,
    enumerate_tasks,
    has_outstanding_tasks,
    parse_task_counts,
)


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
        text = "- [ ] Real unchecked\n\n~~~\n- [ ] fake inside tilde fence\n~~~\n"
        assert parse_task_counts(text) == (0, 1)

    def test_indented_subtasks_counted(self):
        text = (
            "- [ ] Parent task\n  - [ ] Indented subtask\n\t- [x] Tab-indented done\n"
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
        text = '- [x] Real task done\n\n```bash\necho "- [ ] Fake" > plan.md\n```\n'
        (tmp_path / "PLAN.md").write_text(text)
        assert has_outstanding_tasks(tmp_path) is False


def _expected_id(text: str) -> str:
    return "t-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


class TestEnumerateTasks:
    def test_empty_plan_missing_file(self, tmp_path):
        assert enumerate_tasks(tmp_path) == []

    def test_empty_plan_no_tasks(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("# Heading\n\nProse only, no checkboxes.\n")
        assert enumerate_tasks(tmp_path) == []

    def test_basic_enumeration(self, tmp_path):
        text = "- [ ] First task\n- [x] Second task\n- [ ] Third task\n"
        (tmp_path / "PLAN.md").write_text(text)
        tasks = enumerate_tasks(tmp_path)
        assert len(tasks) == 3
        assert all(isinstance(t, Task) for t in tasks)
        assert tasks[0].text == "First task"
        assert tasks[0].line_no == 1
        assert tasks[0].checked is False
        assert tasks[0].task_id == _expected_id("First task")
        assert tasks[1].text == "Second task"
        assert tasks[1].line_no == 2
        assert tasks[1].checked is True
        assert tasks[2].line_no == 3

    def test_stable_ids_across_reorderings(self, tmp_path):
        """A task's id depends on its text, not its position in the file."""
        text_a = "- [ ] Alpha\n- [ ] Beta\n- [ ] Gamma\n"
        text_b = "- [ ] Gamma\n- [ ] Alpha\n- [ ] Beta\n"
        (tmp_path / "PLAN.md").write_text(text_a)
        tasks_a = {t.text: t.task_id for t in enumerate_tasks(tmp_path)}
        (tmp_path / "PLAN.md").write_text(text_b)
        tasks_b = {t.text: t.task_id for t in enumerate_tasks(tmp_path)}
        assert tasks_a == tasks_b

    def test_collision_suffixing(self, tmp_path):
        """Duplicate task text gets `-2`, `-3`, ... suffixes in order."""
        text = (
            "- [ ] Same task\n- [ ] Same task\n- [ ] Same task\n- [ ] Different task\n"
        )
        (tmp_path / "PLAN.md").write_text(text)
        tasks = enumerate_tasks(tmp_path)
        assert len(tasks) == 4
        base = _expected_id("Same task")
        assert tasks[0].task_id == base
        assert tasks[1].task_id == f"{base}-2"
        assert tasks[2].task_id == f"{base}-3"
        assert tasks[3].task_id == _expected_id("Different task")

    def test_fenced_block_skipped(self, tmp_path):
        """Checkboxes inside fenced code blocks must not be enumerated."""
        text = (
            "- [ ] Real task\n"
            "\n"
            "```bash\n"
            "- [ ] Fake task in fence\n"
            "```\n"
            "- [x] Another real task\n"
            "\n"
            "~~~\n"
            "- [ ] Fake task in tilde fence\n"
            "~~~\n"
        )
        (tmp_path / "PLAN.md").write_text(text)
        tasks = enumerate_tasks(tmp_path)
        assert [t.text for t in tasks] == ["Real task", "Another real task"]
        assert tasks[0].line_no == 1
        assert tasks[1].line_no == 6

    def test_text_strips_trailing_whitespace(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [ ] Task with trailing space   \n")
        tasks = enumerate_tasks(tmp_path)
        assert tasks[0].text == "Task with trailing space"

    def test_indented_subtasks_enumerated(self, tmp_path):
        text = "- [ ] Parent\n  - [ ] Child\n\t- [x] Tab child\n"
        (tmp_path / "PLAN.md").write_text(text)
        tasks = enumerate_tasks(tmp_path)
        assert [t.text for t in tasks] == ["Parent", "Child", "Tab child"]
        assert [t.checked for t in tasks] == [False, False, True]

    def test_tasks_are_frozen(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [ ] One\n")
        task = enumerate_tasks(tmp_path)[0]
        import pytest

        with pytest.raises(Exception):
            task.text = "mutated"  # type: ignore[misc]
