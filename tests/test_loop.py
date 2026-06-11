"""Tests for loop helpers."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ola.agents.base import Agent, AgentResponse
from ola.agents.claude_code import ClaudeCodeAgent
from ola.loop import (
    _append_stats,
    _git_commit,
    _initial_concurrency,
    _process_folder,
    per_task_state_dir,
)
from ola.monitor.data import parse_stats_jsonl
from ola.stats import IterationStats


class _FakeAgent(Agent):
    mnemonic = "cc"

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        raise NotImplementedError

    def version(self):
        return "1.5.0"


def _read_record(tmp_path):
    """Read the single JSON line from STATS.jsonl."""
    text = (tmp_path / "STATS.jsonl").read_text()
    return json.loads(text.strip())


def _read_records(tmp_path):
    """Read all JSON lines from STATS.jsonl."""
    text = (tmp_path / "STATS.jsonl").read_text()
    return [json.loads(line) for line in text.strip().splitlines()]


# --- _append_stats tests ---


def test_append_stats_basic_record(tmp_path):
    """All IterationStats fields + phase + wall_ms are present; no agent fields."""
    stats = IterationStats(
        input_tokens=500,
        output_tokens=200,
        cache_read_tokens=100,
        cache_creation_tokens=50,
        num_turns=3,
        models=["claude-3-opus"],
        tool_ms=1000,
        llm_ms=3000,
        max_input_tokens=800,
        ttft_ms=150,
        streamed=True,
    )
    _append_stats(tmp_path, "task-t1-1", stats, wall_ms=5000)
    rec = _read_record(tmp_path)

    assert rec["phase"] == "task-t1-1"
    assert rec["wall_ms"] == 5000
    assert rec["input_tokens"] == 500
    assert rec["output_tokens"] == 200
    assert rec["cache_read_tokens"] == 100
    assert rec["cache_creation_tokens"] == 50
    assert rec["num_turns"] == 3
    assert rec["models"] == ["claude-3-opus"]
    assert rec["tool_ms"] == 1000  # already set, not derived
    assert rec["llm_ms"] == 3000
    assert rec["max_input_tokens"] == 800
    assert rec["ttft_ms"] == 150
    assert rec["streamed"] is True
    # No agent passed → agent fields absent
    assert "agent" not in rec
    assert "agent_version" not in rec
    # Default task tracking
    assert rec["tasks_completed"] == 0
    assert rec["tasks_total"] == 0
    assert rec["tasks_completed_delta"] == 0


def test_append_stats_tool_ms_derived(tmp_path):
    """tool_ms is derived from wall_ms - llm_ms when tool_ms is 0."""
    stats = IterationStats(llm_ms=3000, tool_ms=0)
    _append_stats(tmp_path, "task-t1-1", stats, wall_ms=5000)
    rec = _read_record(tmp_path)
    assert rec["tool_ms"] == 2000


def test_append_stats_tool_ms_not_overridden(tmp_path):
    """tool_ms is NOT overridden when already set."""
    stats = IterationStats(llm_ms=3000, tool_ms=1500)
    _append_stats(tmp_path, "task-t1-1", stats, wall_ms=5000)
    rec = _read_record(tmp_path)
    assert rec["tool_ms"] == 1500


def test_append_stats_tool_ms_clamped(tmp_path):
    """tool_ms is clamped to 0 when llm_ms > wall_ms."""
    stats = IterationStats(llm_ms=6000, tool_ms=0)
    _append_stats(tmp_path, "task-t1-1", stats, wall_ms=5000)
    rec = _read_record(tmp_path)
    assert rec["tool_ms"] == 0


def test_append_stats_with_agent(tmp_path):
    """Agent mnemonic and version are written when agent is provided."""
    stats = IterationStats()
    agent = _FakeAgent()
    _append_stats(tmp_path, "task-t1-1", stats, wall_ms=1000, agent=agent)
    rec = _read_record(tmp_path)
    assert rec["agent"] == "cc"
    assert rec["agent_version"] == "1.5.0"


def test_append_stats_no_agent(tmp_path):
    """Agent fields are absent when no agent is provided."""
    stats = IterationStats()
    _append_stats(tmp_path, "task-t1-1", stats, wall_ms=1000)
    rec = _read_record(tmp_path)
    assert "agent" not in rec
    assert "agent_version" not in rec


def test_append_stats_task_tracking(tmp_path):
    """Task tracking fields are computed from before/after tuples."""
    stats = IterationStats()
    _append_stats(
        tmp_path,
        "loop-1",
        stats,
        wall_ms=1000,
        tasks_before=(2, 5),
        tasks_after=(4, 5),
    )
    rec = _read_record(tmp_path)
    assert rec["tasks_completed"] == 4
    assert rec["tasks_total"] == 5
    assert rec["tasks_completed_delta"] == 2


def test_append_stats_appends_multiple(tmp_path):
    """Multiple calls append separate JSON lines."""
    _append_stats(tmp_path, "task-t1-1", IterationStats(), wall_ms=1000)
    _append_stats(tmp_path, "loop-1", IterationStats(), wall_ms=2000)
    recs = _read_records(tmp_path)
    assert len(recs) == 2
    assert recs[0]["phase"] == "task-t1-1"
    assert recs[1]["phase"] == "loop-1"


# --- Roundtrip contract test ---


def test_stats_roundtrip_contract(tmp_path):
    """Write via _append_stats, read via parse_stats_jsonl — all fields survive."""
    stats = IterationStats(
        input_tokens=1000,
        output_tokens=400,
        cache_read_tokens=300,
        cache_creation_tokens=100,
        num_turns=5,
        models=["claude-3-opus", "claude-3-sonnet"],
        tool_ms=2000,  # set explicitly to avoid derivation
        llm_ms=4000,
        max_input_tokens=1500,
        ttft_ms=250,
        streamed=True,
    )
    agent = _FakeAgent()
    _append_stats(
        tmp_path,
        "loop-1",
        stats,
        wall_ms=8000,
        agent=agent,
        tasks_before=(1, 5),
        tasks_after=(3, 5),
    )

    text = (tmp_path / "STATS.jsonl").read_text()
    iterations = parse_stats_jsonl(text)
    assert len(iterations) == 1
    it = iterations[0]

    # Phase and timing
    assert it.phase == "loop-1"
    assert it.wall_ms == 8000

    # Token fields
    assert it.input_tokens == 1000
    assert it.output_tokens == 400
    assert it.cache_read_tokens == 300
    assert it.cache_creation_tokens == 100

    # Turns and models
    assert it.num_turns == 5
    assert it.models == ["claude-3-opus", "claude-3-sonnet"]

    # Timing fields
    assert it.tool_ms == 2000
    assert it.llm_ms == 4000
    assert it.max_input_tokens == 1500
    assert it.ttft_ms == 250
    assert it.streamed is True

    # Agent fields
    assert it.agent == "cc"
    assert it.agent_version == "1.5.0"

    # Task fields
    assert it.tasks_completed == 3
    assert it.tasks_total == 5
    assert it.tasks_completed_delta == 2


# --- _initial_concurrency tests ---


def test_initial_concurrency_missing_file_defaults_to_one(tmp_path):
    assert _initial_concurrency(tmp_path) == 1


def test_initial_concurrency_reads_integer(tmp_path):
    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("4\n")
    assert _initial_concurrency(tmp_path) == 4


def test_initial_concurrency_malformed_defaults(tmp_path):
    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("not a number")
    assert _initial_concurrency(tmp_path) == 1


def test_initial_concurrency_rejects_non_positive(tmp_path):
    (tmp_path / ".ola").mkdir()
    (tmp_path / ".ola" / "concurrency").write_text("0")
    assert _initial_concurrency(tmp_path) == 1


# --- End-to-end roundtrip sentinel ---


def _make_proc(lines: list[str], returncode: int = 0) -> MagicMock:
    """Return a mock Popen whose stdout yields *lines* as NDJSON."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter(line + "\n" for line in lines)
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = ""
    proc.returncode = returncode
    proc.wait.return_value = returncode
    proc.kill = MagicMock()
    return proc


def _stream_event(inner: dict) -> str:
    return json.dumps({"type": "stream_event", "event": inner})


def _cc_stream_lines() -> list[str]:
    """Canned CC stream with --include-partial-messages output (two turns)."""
    msg_start_1 = _stream_event(
        {
            "type": "message_start",
            "message": {
                "model": "claude-sonnet-4-20250514",
                "usage": {
                    "input_tokens": 5,
                    "cache_creation_input_tokens": 6663,
                    "cache_read_input_tokens": 15771,
                },
            },
        }
    )
    cbs_1 = _stream_event({"type": "content_block_start"})
    md_1 = _stream_event({"type": "message_delta"})

    msg_start_2 = _stream_event(
        {
            "type": "message_start",
            "message": {
                "model": "claude-sonnet-4-20250514",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 5000,
                },
            },
        }
    )
    cbs_2 = _stream_event({"type": "content_block_start"})
    md_2 = _stream_event({"type": "message_delta"})

    result = json.dumps(
        {
            "type": "result",
            "result": "Done.",
            "subtype": "success",
            "num_turns": 2,
            "usage": {
                "input_tokens": 200,
                "output_tokens": 80,
                "cache_creation_input_tokens": 6763,
                "cache_read_input_tokens": 20771,
            },
        }
    )

    return [
        json.dumps({"type": "system"}),
        msg_start_1,
        cbs_1,
        md_1,
        msg_start_2,
        cbs_2,
        md_2,
        result,
    ]


def test_cc_stream_to_stats_jsonl_roundtrip(tmp_path):
    """Regression sentinel: CC stream → IterationStats → STATS.jsonl → parse.

    Asserts that every column ola-top displays would render non-zero values
    for the fields that were silently broken by dbcc23b: models,
    max_input_tokens, ttft_ms, llm_ms.
    """
    # Simulate wall-clock time so TTFT and decode are non-zero.
    # Per turn: message_start → content_block_start → message_delta
    # Turn 1: ttft = 0.100s, decode = 0.200s
    # Turn 2: ttft = 0.150s, decode = 0.250s
    clock = iter(
        [
            0.0,  # turn 1 message_start  → turn_start
            0.100,  # turn 1 content_block_start → token_start (ttft=100ms)
            0.300,  # turn 1 message_delta (decode=200ms)
            0.500,  # turn 2 message_start  → turn_start
            0.650,  # turn 2 content_block_start → token_start (ttft=150ms)
            0.900,  # turn 2 message_delta (decode=250ms)
        ]
    )

    # Step 1: Run _stream() on a mocked CC process with faked time.
    proc = _make_proc(_cc_stream_lines())
    agent = ClaudeCodeAgent()
    with patch("ola.agents.claude_code.time") as mock_time:
        mock_time.monotonic = lambda: next(clock)
        response = agent._stream(proc, "test prompt")
    stats = response.stats

    # Step 2: Write via _append_stats.
    _append_stats(
        tmp_path,
        "loop-1",
        stats,
        wall_ms=10000,
        agent=agent,
        tasks_before=(0, 3),
        tasks_after=(1, 3),
    )

    # Step 3: Read back via parse_stats_jsonl.
    text = (tmp_path / "STATS.jsonl").read_text()
    iterations = parse_stats_jsonl(text)
    assert len(iterations) == 1
    it = iterations[0]

    # Step 4: The four fields that dbcc23b silently zeroed MUST be non-zero.
    assert it.models, "models must not be empty"
    assert it.max_input_tokens > 0, "max_input_tokens must be non-zero"
    assert it.ttft_ms > 0, "ttft_ms must be non-zero"
    assert it.llm_ms > 0, "llm_ms must be non-zero"

    # Verify specific values for extra confidence.
    assert "claude-sonnet-4-20250514" in it.models
    # max_input_tokens should be the larger turn: 5 + 6663 + 15771 = 22439
    assert it.max_input_tokens == 5 + 6663 + 15771
    # ttft_ms = 100 + 150 = 250, llm_ms = ttft + decode(200+250) ≈ 700
    # Allow ±1ms for int() truncation of float arithmetic.
    assert abs(it.ttft_ms - 250) <= 1
    assert abs(it.llm_ms - 700) <= 1

    # tool_ms should be derived (wall_ms - llm_ms) and positive.
    assert it.tool_ms == 10000 - it.llm_ms

    # Token fields from result.usage should survive the roundtrip.
    # input_tokens = raw(200) + cache_creation(6763) + cache_read(20771)
    assert it.input_tokens == 200 + 6763 + 20771
    assert it.output_tokens == 80


# --- _process_folder dispatch tests ---
#
# The per-iteration inner loop is gone: _process_folder hands the folder to
# scheduler.run_folder. Task lifecycle, the stagnation backstop, and
# rate-limit sleep-and-resume are exercised in tests/test_scheduler.py.


def test_process_folder_dispatches_to_scheduler(tmp_path):
    """With PLAN.md present, _process_folder hands the folder to the scheduler."""
    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "PLAN.md").write_text("- [ ] Task A\n")

    agent = _FakeAgent()
    with patch("ola.scheduler.run_folder") as mock_run:
        _process_folder(agent, folder, limit=None, agent_root=tmp_path)

    mock_run.assert_called_once()
    args = mock_run.call_args.args
    assert args[0] is agent
    assert args[1] == folder
    assert args[2] == tmp_path
    assert args[3] == 1  # default cap


def test_process_folder_passes_concurrency_cap(tmp_path):
    """The cap from .ola/concurrency is forwarded as the scheduler's initial_cap."""
    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "PLAN.md").write_text("- [ ] Task A\n")
    (folder / ".ola").mkdir()
    (folder / ".ola" / "concurrency").write_text("3\n")

    agent = _FakeAgent()
    with patch("ola.scheduler.run_folder") as mock_run:
        _process_folder(agent, folder, limit=None, agent_root=tmp_path)

    assert mock_run.call_args.args[3] == 3


def test_process_folder_skips_when_no_plan(tmp_path, caplog):
    """No PLAN.md → warn and don't dispatch."""
    folder = tmp_path / "phase"
    folder.mkdir()

    agent = _FakeAgent()
    with (
        caplog.at_level(logging.WARNING, logger="ola.loop"),
        patch("ola.scheduler.run_folder") as mock_run,
    ):
        _process_folder(agent, folder, limit=None, agent_root=tmp_path)

    mock_run.assert_not_called()
    assert any("no PLAN.md" in rec.message for rec in caplog.records)


# --- Emitter wiring tests ---


def test_build_emitter_local_only(tmp_path):
    """The emitter attaches a single LocalSink for the folder's audit trail."""
    from ola.events.client import LocalSink

    from ola.loop import _build_emitter

    folder = tmp_path / "phase"
    folder.mkdir()
    emitter = _build_emitter(folder)
    try:
        assert len(emitter._sinks) == 1
        assert isinstance(emitter._sinks[0], LocalSink)
        assert emitter._sinks[0]._path == folder / ".ola" / "events.jsonl"
    finally:
        emitter.close()


def test_process_folder_passes_emitter_to_scheduler(tmp_path):
    """_process_folder builds an emitter and forwards it to run_folder."""
    from ola.events.client import Emitter

    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "PLAN.md").write_text("- [ ] Task A\n")

    agent = _FakeAgent()
    with patch("ola.scheduler.run_folder") as mock_run:
        _process_folder(agent, folder, limit=None, agent_root=tmp_path)

    emitter = mock_run.call_args.kwargs["emitter"]
    assert isinstance(emitter, Emitter)


def test_process_folder_writes_events_jsonl(tmp_path):
    """End-to-end: a real scheduler run produces an events.jsonl audit trail."""
    import subprocess

    def _git(cwd, *args):
        subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)

    # An agent repo with one folder containing a single task.
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.com")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "PLAN.md").write_text("- [ ] Task A\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    class _TickAgent(Agent):
        mnemonic = "cc"

        def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
            # Tick the single task's checkbox in the worktree PLAN.md. The
            # prompt mentions "PLAN.md" in prose too, so match the path token.
            for token in prompt.split():
                if token.endswith("PLAN.md") and "/" in token:
                    p = Path(token)
                    p.write_text(p.read_text().replace("- [ ]", "- [x]"))
                    break
            return AgentResponse(output="done", success=True, stats=IterationStats())

        def version(self):
            return "1.0.0"

    _process_folder(_TickAgent(), folder, limit=None, agent_root=tmp_path)

    events_file = folder / ".ola" / "events.jsonl"
    assert events_file.exists()
    statuses = [
        json.loads(line)["status"]
        for line in events_file.read_text().splitlines()
        if line.strip()
    ]
    assert "started" in statuses
    assert "complete" in statuses


# --- Stale git lock tests ---


def test_git_commit_removes_stale_lock(tmp_path, caplog):
    """_git_commit removes index.lock and logs a warning when it exists."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    lock = git_dir / "index.lock"
    lock.write_text("")

    with (
        caplog.at_level(logging.WARNING, logger="ola.loop"),
        patch("ola.loop._git"),
        patch("ola.loop.subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        _git_commit(tmp_path, "test message")

    assert not lock.exists()
    assert any("stale" in rec.message.lower() for rec in caplog.records)


def test_git_commit_no_lock_no_warning(tmp_path, caplog):
    """_git_commit does not warn when no stale lock exists."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    with (
        caplog.at_level(logging.WARNING, logger="ola.loop"),
        patch("ola.loop._git"),
        patch("ola.loop.subprocess.run", return_value=MagicMock(returncode=0)),
    ):
        _git_commit(tmp_path, "test message")

    assert not any("stale" in rec.message.lower() for rec in caplog.records)


# --- per_task_state_dir tests ---


class _StatelessAgent(Agent):
    """Agent with no state directory (state_dir_name == '')."""

    mnemonic = "ss"
    state_dir_name = ""

    def run(self, prompt, workdir, state_dir=None, labels=None, on_progress=None):
        raise NotImplementedError

    def version(self):
        return ""


def test_per_task_state_dir_returns_none_when_no_state_dir(tmp_path):
    """Agents with state_dir_name == '' get None — no directory is created."""
    folder = tmp_path / "phase"
    folder.mkdir()
    result = per_task_state_dir(folder, _StatelessAgent(), "t-abc1234")
    assert result is None
    # Nothing was created under the folder
    assert list(folder.iterdir()) == []


def test_per_task_state_dir_creates_isolated_dir(tmp_path):
    """Agents with a state_dir_name get <folder>/<name>/<task_id>/, created on disk."""
    folder = tmp_path / "phase"
    folder.mkdir()
    agent = ClaudeCodeAgent()  # state_dir_name == ".claude"

    result = per_task_state_dir(folder, agent, "t-abc1234")

    assert result == str(folder / ".claude" / "t-abc1234")
    assert (folder / ".claude" / "t-abc1234").is_dir()


def test_per_task_state_dir_distinct_per_task_id(tmp_path):
    """Two task ids under the same folder get independent directories."""
    folder = tmp_path / "phase"
    folder.mkdir()
    agent = ClaudeCodeAgent()

    p1 = per_task_state_dir(folder, agent, "t-aaaa")
    p2 = per_task_state_dir(folder, agent, "t-bbbb")

    assert p1 != p2
    assert Path(p1).is_dir()
    assert Path(p2).is_dir()
    # Both live under the shared .claude/ directory
    assert Path(p1).parent == Path(p2).parent == folder / ".claude"


def test_per_task_state_dir_idempotent(tmp_path):
    """Calling twice with the same task_id is safe and returns the same path."""
    folder = tmp_path / "phase"
    folder.mkdir()
    agent = ClaudeCodeAgent()

    p1 = per_task_state_dir(folder, agent, "t-abc1234")
    # Write a marker into the dir so we can confirm it isn't wiped on second call
    (Path(p1) / "marker").write_text("keep")
    p2 = per_task_state_dir(folder, agent, "t-abc1234")

    assert p1 == p2
    assert (Path(p1) / "marker").read_text() == "keep"


# --- .ola/ git exclusion ---


def test_exclude_ola_artifacts_appends_once(tmp_path):
    import subprocess

    from ola.loop import _exclude_ola_artifacts

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    _exclude_ola_artifacts(tmp_path)
    _exclude_ola_artifacts(tmp_path)  # idempotent

    exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert exclude.splitlines().count(".ola/") == 1


def test_exclude_ola_artifacts_noop_without_git(tmp_path):
    from ola.loop import _exclude_ola_artifacts

    _exclude_ola_artifacts(tmp_path)  # no .git → silently does nothing
    assert not (tmp_path / ".git").exists()


# --- BLOCKERS.md folders are skipped quietly ---


def test_process_folder_skips_blockers_folder(tmp_path, caplog):
    folder = tmp_path / "01b-init-blockers"
    folder.mkdir()
    (folder / "BLOCKERS.md").write_text("# Needs a human\n")

    agent = _FakeAgent()
    with caplog.at_level(logging.INFO, logger="ola.loop"):
        _process_folder(agent, folder, None, tmp_path)

    assert any("awaiting human input" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# --- run_outer_loop re-discovery ---


def test_run_outer_loop_picks_up_midrun_sibling_before_later_folders(tmp_path):
    """A 01a- folder created while 01- runs is processed before 02-."""
    from ola.loop import run_outer_loop

    for name in ("01-init", "02-utils"):
        d = tmp_path / name
        d.mkdir()
        (d / "PLAN.md").write_text("- [x] done\n")

    order: list[str] = []

    def fake_process(
        agent, folder, limit, agent_root, max_attempts=0, janitor_enabled=True
    ):
        order.append(folder.name)
        if folder.name == "01-init":
            sibling = tmp_path / "01a-init-leftovers"
            sibling.mkdir()
            (sibling / "PLAN.md").write_text("- [x] moved task\n")

    agent = _FakeAgent()
    with (
        patch("ola.loop._process_folder", side_effect=fake_process),
        patch("ola.loop._load_agent_env"),
        patch("ola.loop._ensure_git"),
    ):
        run_outer_loop(agent, tmp_path)

    assert order == ["01-init", "01a-init-leftovers", "02-utils"]


def test_run_outer_loop_bails_when_folder_left_unfinished(tmp_path):
    """A folder that drains with unticked checkboxes stops the run; later
    folders are never reached."""
    from ola.loop import run_outer_loop
    from ola.scheduler import FolderIncompleteError

    for name in ("01-init", "02-utils"):
        d = tmp_path / name
        d.mkdir()
        (d / "PLAN.md").write_text("- [ ] not done\n")

    order: list[str] = []

    def fake_process(
        agent, folder, limit, agent_root, max_attempts=0, janitor_enabled=True
    ):
        # Simulate a stuck folder: run_folder drained but ticked nothing.
        order.append(folder.name)

    agent = _FakeAgent()
    with (
        patch("ola.loop._process_folder", side_effect=fake_process),
        patch("ola.loop._load_agent_env"),
        patch("ola.loop._ensure_git"),
        pytest.raises(FolderIncompleteError) as excinfo,
    ):
        run_outer_loop(agent, tmp_path)

    assert excinfo.value.folder_name == "01-init"
    assert excinfo.value.remaining == 1
    # Bailed on the first folder; 02-utils never processed.
    assert order == ["01-init"]


# --- end-of-run attention summary ---


def test_log_attention_summary_reports_blockers_and_blocked_tasks(tmp_path, caplog):
    from ola.loop import _log_attention_summary

    blockers = tmp_path / "01b-init-blockers"
    blockers.mkdir()
    (blockers / "BLOCKERS.md").write_text("# Needs a human\n")

    folder = tmp_path / "01-init"
    (folder / ".ola").mkdir(parents=True)
    (folder / ".ola" / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "t-abc",
                        "text": "Call the FOO API",
                        "line_no": 1,
                        "status": "blocked",
                        "attempts": 1,
                        "last_error": "blocked: missing key",
                    }
                ]
            }
        )
    )

    with caplog.at_level(logging.WARNING, logger="ola.loop"):
        _log_attention_summary(tmp_path)

    messages = [r.message for r in caplog.records]
    assert any("Human attention needed" in m for m in messages)
    assert any("BLOCKERS.md" in m for m in messages)
    assert any("Call the FOO API" in m and "missing key" in m for m in messages)


def test_log_attention_summary_silent_when_clean(tmp_path, caplog):
    from ola.loop import _log_attention_summary

    (tmp_path / "01-init").mkdir()
    with caplog.at_level(logging.WARNING, logger="ola.loop"):
        _log_attention_summary(tmp_path)
    assert caplog.records == []
