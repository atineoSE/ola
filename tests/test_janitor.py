"""Tests for ola.janitor — sibling-name allocation and prompt building."""

import pytest

from ola.blocked import BlockedRecord
from ola.janitor import allocate_sibling_names, build_janitor_prompt, load_contract


def _mkdirs(root, *names):
    for name in names:
        (root / name).mkdir()


class TestAllocateSiblingNames:
    def test_first_allocation(self, tmp_path):
        _mkdirs(tmp_path, "01-init", "02-utils")
        left, block = allocate_sibling_names(tmp_path, "01-init")
        assert left == "01a-init-leftovers"
        assert block == "01b-init-blockers"

    def test_skips_used_suffixes(self, tmp_path):
        _mkdirs(tmp_path, "01-init", "01a-init-leftovers", "01b-init-blockers")
        left, block = allocate_sibling_names(tmp_path, "01-init")
        assert left == "01c-init-leftovers"
        assert block == "01d-init-blockers"

    def test_strips_leftovers_from_base(self, tmp_path):
        """A janitor inside a leftovers folder never stacks -leftovers-leftovers."""
        _mkdirs(tmp_path, "01-init", "01a-init-leftovers")
        left, block = allocate_sibling_names(tmp_path, "01a-init-leftovers")
        assert left == "01b-init-leftovers"
        assert block == "01c-init-blockers"

    def test_other_indices_do_not_consume_suffixes(self, tmp_path):
        _mkdirs(tmp_path, "01-init", "02a-utils-leftovers", "02-utils")
        left, _ = allocate_sibling_names(tmp_path, "01-init")
        assert left == "01a-init-leftovers"

    def test_allocated_names_sort_between_parent_and_next(self, tmp_path):
        _mkdirs(tmp_path, "01-init", "02-utils")
        left, block = allocate_sibling_names(tmp_path, "01-init")
        assert "01-init" < left < block < "02-utils"

    def test_z_overflow_keeps_sort_order(self, tmp_path):
        """Past 'z', suffixes extend (za, zb, …) and still sort before 02-."""
        _mkdirs(tmp_path, "01-init")
        for letter in "abcdefghijklmnopqrstuvwxyz":
            (tmp_path / f"01{letter}-init-leftovers").mkdir()
        left, block = allocate_sibling_names(tmp_path, "01-init")
        assert left == "01za-init-leftovers"
        assert block == "01zb-init-blockers"
        assert "01z-init-leftovers" < left < block < "02-utils"

    def test_unparsable_folder_name_raises(self, tmp_path):
        with pytest.raises(ValueError):
            allocate_sibling_names(tmp_path, "not-numbered")


class TestBuildJanitorPrompt:
    def test_substitutes_everything(self, tmp_path):
        _mkdirs(tmp_path, "01-init")
        folder = tmp_path / "01-init"
        record = BlockedRecord(
            task_id="t-abc12345",
            reason="missing FOO_API_KEY",
            ts="2026-06-10T00:00:00Z",
        )
        prompt = build_janitor_prompt(folder, tmp_path, record, "Call the FOO API")

        assert "{{" not in prompt  # every placeholder resolved
        assert "Call the FOO API" in prompt
        assert "t-abc12345" in prompt
        assert "missing FOO_API_KEY" in prompt
        assert str(folder / "PLAN.md") in prompt
        assert "`01a-init-leftovers`" in prompt
        assert "`01b-init-blockers`" in prompt
        # The canonical contract is inlined verbatim.
        assert load_contract().rstrip() in prompt

    def test_leftovers_folder_gets_escalate_hint(self, tmp_path):
        _mkdirs(tmp_path, "01a-init-leftovers")
        folder = tmp_path / "01a-init-leftovers"
        record = BlockedRecord(task_id="t-1", reason="still missing", ts="")
        prompt = build_janitor_prompt(folder, tmp_path, record, "Task X")
        assert "prefer ESCALATE" in prompt

    def test_plain_folder_has_no_escalate_hint(self, tmp_path):
        _mkdirs(tmp_path, "01-init")
        record = BlockedRecord(task_id="t-1", reason="r", ts="")
        prompt = build_janitor_prompt(tmp_path / "01-init", tmp_path, record, "Task X")
        assert "prefer ESCALATE" not in prompt
