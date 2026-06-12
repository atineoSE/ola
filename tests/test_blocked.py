"""Tests for ola.blocked — the ola-blocked escape-hatch plumbing."""

import os
import subprocess

import pytest

from ola.blocked import (
    BlockedRecord,
    clear_blocked_record,
    provision_blocked_script,
    read_blocked_record,
)


def test_provision_writes_executable_script(tmp_path):
    worktree = tmp_path / "wt"
    folder = tmp_path / "agent-folder"
    folder.mkdir()
    worktree.mkdir()

    script = provision_blocked_script(worktree, folder, "t-abc12345")

    assert script == worktree / ".ola" / "bin" / "ola-blocked"
    assert script.exists()
    assert script.stat().st_mode & 0o111  # executable
    content = script.read_text()
    assert content.startswith("#!/bin/sh")
    assert "t-abc12345" in content


def test_running_script_lands_marker_in_agent_folder(tmp_path):
    """Executing the provisioned script end-to-end writes the reason marker."""
    worktree = tmp_path / "wt"
    folder = tmp_path / "agent-folder"
    folder.mkdir()
    worktree.mkdir()
    script = provision_blocked_script(worktree, folder, "t-abc12345")

    result = subprocess.run(
        [str(script), "--reason", "missing FOO_API_KEY"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "BLOCKED" in result.stdout
    record = read_blocked_record(folder, "t-abc12345")
    assert record is not None
    assert record == BlockedRecord(
        task_id="t-abc12345", reason="missing FOO_API_KEY", ts=record.ts
    )
    assert record.ts  # ISO timestamp derived from mtime
    # The marker lives under the agent folder, not the worktree, so it
    # survives worktree cleanup.
    assert (folder / ".ola" / "blocked" / "t-abc12345.reason").exists()
    assert not (worktree / ".ola" / "blocked").exists()


def test_read_returns_none_without_marker(tmp_path):
    assert read_blocked_record(tmp_path, "t-missing") is None


def test_clear_removes_marker_and_is_idempotent(tmp_path):
    worktree = tmp_path / "wt"
    folder = tmp_path / "agent-folder"
    folder.mkdir()
    worktree.mkdir()
    script = provision_blocked_script(worktree, folder, "t-abc12345")
    subprocess.run([str(script), "--reason", "x"], capture_output=True, check=True)
    assert read_blocked_record(folder, "t-abc12345") is not None

    clear_blocked_record(folder, "t-abc12345")
    assert read_blocked_record(folder, "t-abc12345") is None
    clear_blocked_record(folder, "t-abc12345")  # no-op, no raise


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses write-permission bits, so the marker write never fails",
)
def test_script_fails_loudly_when_marker_unwritable(tmp_path):
    """A write that fails must error and NOT falsely claim BLOCKED.

    Regression guard: Claude Code's command sandbox once denied this write
    (the marker lands above the worktree cwd), but the script still printed
    "recorded as BLOCKED", so the agent stopped while the harness saw no
    marker and retried to exhaustion. The failure must now be visible.
    """
    worktree = tmp_path / "wt"
    folder = tmp_path / "agent-folder"
    folder.mkdir()
    worktree.mkdir()
    script = provision_blocked_script(worktree, folder, "t-abc12345")

    # Make the agent folder unwritable so `mkdir -p .ola/blocked` fails, the
    # same way the sandbox denied the cross-worktree write in a real run.
    folder.chmod(0o500)
    try:
        result = subprocess.run(
            [str(script), "--reason", "missing FOO_API_KEY"],
            capture_output=True,
            text=True,
        )
    finally:
        folder.chmod(0o700)

    assert result.returncode != 0
    assert "BLOCKED" not in result.stdout
    assert "ERROR" in result.stderr
    assert read_blocked_record(folder, "t-abc12345") is None


def test_script_without_reason_records_empty_reason(tmp_path):
    """A bare invocation still records the blockage (reason empty)."""
    worktree = tmp_path / "wt"
    folder = tmp_path / "agent-folder"
    folder.mkdir()
    worktree.mkdir()
    script = provision_blocked_script(worktree, folder, "t-abc12345")
    subprocess.run([str(script)], capture_output=True, check=True)
    record = read_blocked_record(folder, "t-abc12345")
    assert record is not None
    assert record.reason == ""
