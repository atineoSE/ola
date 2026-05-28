"""Regression tests for ola.plan — checkbox parser + Path-based wrappers."""

import hashlib
from pathlib import Path

import pytest

from ola.plan import (
    Task,
    count_tasks,
    enumerate_tasks,
    has_outstanding_tasks,
    parse_task_counts,
    set_task_checked,
    task_is_checked,
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

        with pytest.raises(Exception):
            task.text = "mutated"  # type: ignore[misc]


# --- golden fixtures for set_task_checked ----------------------------------

# Mixed plan: bullets, indented child, fenced block, duplicate text, CRLF
# tolerance, and a final line with no trailing newline.
_GOLDEN_BEFORE = (
    "# Plan\n"
    "\n"
    "- [ ] First task\n"
    "- [x] Done already\n"
    "  - [ ] Indented child\n"
    "\n"
    "```bash\n"
    "- [ ] Not a real task\n"
    "```\n"
    "\n"
    "- [ ] Dup text\n"
    "- [ ] Dup text\n"
    "* [ ] Asterisk last"  # intentionally no trailing newline
)

_GOLDEN_AFTER_TICK_FIRST = (
    "# Plan\n"
    "\n"
    "- [x] First task\n"
    "- [x] Done already\n"
    "  - [ ] Indented child\n"
    "\n"
    "```bash\n"
    "- [ ] Not a real task\n"
    "```\n"
    "\n"
    "- [ ] Dup text\n"
    "- [ ] Dup text\n"
    "* [ ] Asterisk last"
)

_GOLDEN_AFTER_UNTICK_SECOND = (
    "# Plan\n"
    "\n"
    "- [ ] First task\n"
    "- [ ] Done already\n"
    "  - [ ] Indented child\n"
    "\n"
    "```bash\n"
    "- [ ] Not a real task\n"
    "```\n"
    "\n"
    "- [ ] Dup text\n"
    "- [ ] Dup text\n"
    "* [ ] Asterisk last"
)

_GOLDEN_AFTER_TICK_SECOND_DUP = (
    "# Plan\n"
    "\n"
    "- [ ] First task\n"
    "- [x] Done already\n"
    "  - [ ] Indented child\n"
    "\n"
    "```bash\n"
    "- [ ] Not a real task\n"
    "```\n"
    "\n"
    "- [ ] Dup text\n"
    "- [x] Dup text\n"
    "* [ ] Asterisk last"
)


class TestSetTaskChecked:
    def _id(self, folder: Path, text: str) -> str:
        for t in enumerate_tasks(folder):
            if t.text == text:
                return t.task_id
        raise AssertionError(f"task with text {text!r} not present")

    def test_tick_unchecked_matches_golden(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        set_task_checked(tmp_path, self._id(tmp_path, "First task"), True)
        assert plan.read_text() == _GOLDEN_AFTER_TICK_FIRST

    def test_untick_checked_matches_golden(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        set_task_checked(tmp_path, self._id(tmp_path, "Done already"), False)
        assert plan.read_text() == _GOLDEN_AFTER_UNTICK_SECOND

    def test_collision_suffix_targets_second_occurrence(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        tasks = enumerate_tasks(tmp_path)
        dup_ids = [t.task_id for t in tasks if t.text == "Dup text"]
        assert len(dup_ids) == 2 and dup_ids[1].endswith("-2")
        set_task_checked(tmp_path, dup_ids[1], True)
        assert plan.read_text() == _GOLDEN_AFTER_TICK_SECOND_DUP

    def test_idempotent_setting_to_same_state(self, tmp_path):
        """Setting an already-checked task to checked is a safe no-op rewrite."""
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        set_task_checked(tmp_path, self._id(tmp_path, "Done already"), True)
        assert plan.read_text() == _GOLDEN_BEFORE

    def test_unknown_task_id_raises(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        original = plan.read_text()
        with pytest.raises(KeyError):
            set_task_checked(tmp_path, "t-deadbeef", True)
        assert plan.read_text() == original

    def test_fenced_checkbox_not_targetable(self, tmp_path):
        """A checkbox living inside a code fence is not a Task and has no id."""
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        ids = {t.task_id for t in enumerate_tasks(tmp_path)}
        fake = "t-" + hashlib.sha1(b"Not a real task").hexdigest()[:8]
        assert fake not in ids
        with pytest.raises(KeyError):
            set_task_checked(tmp_path, fake, True)

    def test_preserves_indented_child_bullet(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        set_task_checked(tmp_path, self._id(tmp_path, "Indented child"), True)
        out = plan.read_text()
        assert "  - [x] Indented child\n" in out

    def test_preserves_no_trailing_newline_on_last_line(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        set_task_checked(tmp_path, self._id(tmp_path, "Asterisk last"), True)
        out = plan.read_text()
        assert out.endswith("* [x] Asterisk last")
        assert not out.endswith("\n")

    def test_atomic_write_no_tmp_left_behind(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        set_task_checked(tmp_path, self._id(tmp_path, "First task"), True)
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "PLAN.md"]
        assert leftovers == []


class TestTaskIsChecked:
    def _id(self, folder: Path, text: str) -> str:
        for t in enumerate_tasks(folder):
            if t.text == text:
                return t.task_id
        raise AssertionError(f"task with text {text!r} not present")

    def test_unchecked_returns_false(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [ ] Pending task\n")
        assert task_is_checked(tmp_path, self._id(tmp_path, "Pending task")) is False

    def test_checked_returns_true(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [x] Finished task\n")
        assert task_is_checked(tmp_path, self._id(tmp_path, "Finished task")) is True

    def test_uppercase_x_returns_true(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [X] Big X done\n")
        assert task_is_checked(tmp_path, self._id(tmp_path, "Big X done")) is True

    def test_reflects_set_task_checked_round_trip(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        first = self._id(tmp_path, "First task")
        assert task_is_checked(tmp_path, first) is False
        set_task_checked(tmp_path, first, True)
        assert task_is_checked(tmp_path, first) is True
        set_task_checked(tmp_path, first, False)
        assert task_is_checked(tmp_path, first) is False

    def test_collision_suffix_resolved_independently(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        tasks = enumerate_tasks(tmp_path)
        dup_ids = [t.task_id for t in tasks if t.text == "Dup text"]
        assert len(dup_ids) == 2
        assert task_is_checked(tmp_path, dup_ids[0]) is False
        assert task_is_checked(tmp_path, dup_ids[1]) is False
        set_task_checked(tmp_path, dup_ids[1], True)
        assert task_is_checked(tmp_path, dup_ids[0]) is False
        assert task_is_checked(tmp_path, dup_ids[1]) is True

    def test_unknown_task_id_raises_key_error(self, tmp_path):
        (tmp_path / "PLAN.md").write_text("- [ ] Real task\n")
        with pytest.raises(KeyError):
            task_is_checked(tmp_path, "t-deadbeef")

    def test_missing_plan_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            task_is_checked(tmp_path, "t-deadbeef")

    def test_fenced_checkbox_not_targetable(self, tmp_path):
        plan = tmp_path / "PLAN.md"
        plan.write_text(_GOLDEN_BEFORE)
        fake = "t-" + hashlib.sha1(b"Not a real task").hexdigest()[:8]
        with pytest.raises(KeyError):
            task_is_checked(tmp_path, fake)
