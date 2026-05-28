"""Tests for ola.taskstate — TaskState load/sync/mark/save."""

import json

import pytest

from ola.taskstate import TaskEntry, TaskState


def _write_plan(folder, text):
    (folder / "PLAN.md").write_text(text)


class TestLoad:
    def test_missing_file_returns_empty(self, tmp_path):
        state = TaskState.load(tmp_path)
        assert state.all() == []

    def test_missing_file_does_not_create_directory(self, tmp_path):
        TaskState.load(tmp_path)
        assert not (tmp_path / ".ola").exists()

    def test_round_trip(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Alpha\n- [ ] Beta\n")
        state = TaskState.sync_from_plan(tmp_path)
        state.save()

        reloaded = TaskState.load(tmp_path)
        assert [e.text for e in reloaded.all()] == ["Alpha", "Beta"]
        assert all(e.status == "pending" for e in reloaded.all())

    def test_load_rejects_invalid_status(self, tmp_path):
        path = tmp_path / ".ola" / "tasks.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "task_id": "t-abc",
                            "text": "x",
                            "line_no": 1,
                            "status": "bogus",
                            "attempts": 0,
                            "last_error": None,
                        }
                    ]
                }
            )
        )
        with pytest.raises(ValueError):
            TaskState.load(tmp_path)


class TestSyncFromPlan:
    def test_new_plan_yields_pending_entries(self, tmp_path):
        _write_plan(tmp_path, "- [ ] First\n- [ ] Second\n")
        state = TaskState.sync_from_plan(tmp_path)
        entries = state.all()
        assert len(entries) == 2
        assert [e.text for e in entries] == ["First", "Second"]
        assert [e.status for e in entries] == ["pending", "pending"]
        assert all(e.attempts == 0 for e in entries)
        assert all(e.last_error is None for e in entries)

    def test_existing_checkbox_starts_complete(self, tmp_path):
        _write_plan(tmp_path, "- [x] Done already\n- [ ] Pending one\n")
        state = TaskState.sync_from_plan(tmp_path)
        entries = state.all()
        assert entries[0].status == "complete"
        assert entries[1].status == "pending"

    def test_preserves_status_attempts_and_error_across_sync(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Alpha\n- [ ] Beta\n")
        state = TaskState.sync_from_plan(tmp_path)
        alpha_id = state.all()[0].task_id
        state.mark(alpha_id, "failed", attempts=2, last_error="boom")
        state.save()

        # Re-sync: PLAN.md unchanged. Status, attempts, last_error must persist.
        resynced = TaskState.sync_from_plan(tmp_path)
        alpha = resynced.get(alpha_id)
        assert alpha is not None
        assert alpha.status == "failed"
        assert alpha.attempts == 2
        assert alpha.last_error == "boom"

    def test_refreshes_text_and_line_no_on_existing_task(self, tmp_path):
        # Two tasks, same ids both runs (id is sha1 of text)
        _write_plan(tmp_path, "- [ ] Alpha\n- [ ] Beta\n")
        state = TaskState.sync_from_plan(tmp_path)
        beta_id = state.all()[1].task_id
        state.mark(beta_id, "running")
        state.save()

        # Now reorder the file: Beta moves to line 1
        _write_plan(tmp_path, "- [ ] Beta\n- [ ] Alpha\n")
        resynced = TaskState.sync_from_plan(tmp_path)
        beta = resynced.get(beta_id)
        assert beta is not None
        assert beta.line_no == 1
        assert beta.status == "running"  # status preserved
        # Order in `all()` follows PLAN.md after sync
        assert [e.text for e in resynced.all()] == ["Beta", "Alpha"]

    def test_drops_entries_no_longer_in_plan(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Keep me\n- [ ] Drop me\n")
        state = TaskState.sync_from_plan(tmp_path)
        state.save()
        assert len(state.all()) == 2

        _write_plan(tmp_path, "- [ ] Keep me\n")
        resynced = TaskState.sync_from_plan(tmp_path)
        assert [e.text for e in resynced.all()] == ["Keep me"]

    def test_missing_plan_md_yields_empty(self, tmp_path):
        state = TaskState.sync_from_plan(tmp_path)
        assert state.all() == []

    def test_sync_does_not_persist_until_save(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Only one\n")
        TaskState.sync_from_plan(tmp_path)
        assert not (tmp_path / ".ola" / "tasks.json").exists()


class TestMark:
    def test_status_update(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Solo\n")
        state = TaskState.sync_from_plan(tmp_path)
        task_id = state.all()[0].task_id
        state.mark(task_id, "running")
        assert state.get(task_id).status == "running"

    def test_kwargs_update_other_fields(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Solo\n")
        state = TaskState.sync_from_plan(tmp_path)
        task_id = state.all()[0].task_id
        state.mark(task_id, "failed", attempts=3, last_error="oops")
        entry = state.get(task_id)
        assert entry.status == "failed"
        assert entry.attempts == 3
        assert entry.last_error == "oops"

    def test_invalid_status_raises(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Solo\n")
        state = TaskState.sync_from_plan(tmp_path)
        task_id = state.all()[0].task_id
        with pytest.raises(ValueError):
            state.mark(task_id, "bogus")

    def test_unknown_task_id_raises(self, tmp_path):
        state = TaskState(tmp_path)
        with pytest.raises(KeyError):
            state.mark("t-missing", "running")

    def test_unknown_kwarg_raises(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Solo\n")
        state = TaskState.sync_from_plan(tmp_path)
        task_id = state.all()[0].task_id
        with pytest.raises(AttributeError):
            state.mark(task_id, "running", nonsense_field="x")


class TestSave:
    def test_writes_to_dot_ola_tasks_json(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Solo\n")
        state = TaskState.sync_from_plan(tmp_path)
        state.save()
        path = tmp_path / ".ola" / "tasks.json"
        assert path.exists()
        payload = json.loads(path.read_text())
        assert "tasks" in payload
        assert payload["tasks"][0]["text"] == "Solo"
        assert payload["tasks"][0]["status"] == "pending"

    def test_creates_dot_ola_directory(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Solo\n")
        state = TaskState.sync_from_plan(tmp_path)
        assert not (tmp_path / ".ola").exists()
        state.save()
        assert (tmp_path / ".ola").is_dir()

    def test_atomic_save_overwrites_cleanly(self, tmp_path):
        _write_plan(tmp_path, "- [ ] First\n")
        state = TaskState.sync_from_plan(tmp_path)
        state.save()
        first_id = state.all()[0].task_id
        state.mark(first_id, "running")
        state.save()

        payload = json.loads((tmp_path / ".ola" / "tasks.json").read_text())
        assert payload["tasks"][0]["status"] == "running"
        # No leftover tmp file
        assert not (tmp_path / ".ola" / "tasks.json.tmp").exists()


class TestNextPending:
    def test_returns_first_pending(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Alpha\n- [ ] Beta\n")
        state = TaskState.sync_from_plan(tmp_path)
        nxt = state.next_pending()
        assert nxt is not None
        assert nxt.text == "Alpha"

    def test_skips_non_pending_entries(self, tmp_path):
        _write_plan(tmp_path, "- [ ] Alpha\n- [ ] Beta\n")
        state = TaskState.sync_from_plan(tmp_path)
        alpha_id = state.all()[0].task_id
        state.mark(alpha_id, "running")
        nxt = state.next_pending()
        assert nxt is not None
        assert nxt.text == "Beta"

    def test_returns_none_when_no_pending(self, tmp_path):
        _write_plan(tmp_path, "- [x] Done\n")
        state = TaskState.sync_from_plan(tmp_path)
        assert state.next_pending() is None


class TestTaskEntry:
    def test_defaults(self):
        e = TaskEntry(task_id="t-1", text="x", line_no=3)
        assert e.status == "pending"
        assert e.attempts == 0
        assert e.last_error is None
