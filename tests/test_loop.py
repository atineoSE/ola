"""Tests for loop helpers."""

import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from ola.agents.base import Agent, AgentResponse
from ola.agents.claude_code import ClaudeCodeAgent
from ola.loop import (
    _MAX_RATE_LIMIT_WAIT_SEC,
    _MAX_STAGNANT_LOOPS,
    _append_stats,
    _last_loop_number,
    _process_folder,
)
from ola.monitor.data import parse_stats_jsonl
from ola.stats import IterationStats


class _FakeAgent(Agent):
    mnemonic = "cc"

    def run(self, prompt, workdir, state_dir=None, labels=None):
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
    _append_stats(tmp_path, "seed", stats, wall_ms=5000)
    rec = _read_record(tmp_path)

    assert rec["phase"] == "seed"
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
    _append_stats(tmp_path, "seed", stats, wall_ms=5000)
    rec = _read_record(tmp_path)
    assert rec["tool_ms"] == 2000


def test_append_stats_tool_ms_not_overridden(tmp_path):
    """tool_ms is NOT overridden when already set."""
    stats = IterationStats(llm_ms=3000, tool_ms=1500)
    _append_stats(tmp_path, "seed", stats, wall_ms=5000)
    rec = _read_record(tmp_path)
    assert rec["tool_ms"] == 1500


def test_append_stats_tool_ms_clamped(tmp_path):
    """tool_ms is clamped to 0 when llm_ms > wall_ms."""
    stats = IterationStats(llm_ms=6000, tool_ms=0)
    _append_stats(tmp_path, "seed", stats, wall_ms=5000)
    rec = _read_record(tmp_path)
    assert rec["tool_ms"] == 0


def test_append_stats_with_agent(tmp_path):
    """Agent mnemonic and version are written when agent is provided."""
    stats = IterationStats()
    agent = _FakeAgent()
    _append_stats(tmp_path, "seed", stats, wall_ms=1000, agent=agent)
    rec = _read_record(tmp_path)
    assert rec["agent"] == "cc"
    assert rec["agent_version"] == "1.5.0"


def test_append_stats_no_agent(tmp_path):
    """Agent fields are absent when no agent is provided."""
    stats = IterationStats()
    _append_stats(tmp_path, "seed", stats, wall_ms=1000)
    rec = _read_record(tmp_path)
    assert "agent" not in rec
    assert "agent_version" not in rec


def test_append_stats_task_tracking(tmp_path):
    """Task tracking fields are computed from before/after tuples."""
    stats = IterationStats()
    _append_stats(
        tmp_path, "loop-1", stats, wall_ms=1000,
        tasks_before=(2, 5), tasks_after=(4, 5),
    )
    rec = _read_record(tmp_path)
    assert rec["tasks_completed"] == 4
    assert rec["tasks_total"] == 5
    assert rec["tasks_completed_delta"] == 2


def test_append_stats_appends_multiple(tmp_path):
    """Multiple calls append separate JSON lines."""
    _append_stats(tmp_path, "seed", IterationStats(), wall_ms=1000)
    _append_stats(tmp_path, "loop-1", IterationStats(), wall_ms=2000)
    recs = _read_records(tmp_path)
    assert len(recs) == 2
    assert recs[0]["phase"] == "seed"
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
        tmp_path, "loop-1", stats, wall_ms=8000,
        agent=agent, tasks_before=(1, 5), tasks_after=(3, 5),
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


def test_last_loop_number_no_file(tmp_path):
    assert _last_loop_number(tmp_path) == 0


def test_last_loop_number_empty_file(tmp_path):
    (tmp_path / "STATS.jsonl").write_text("")
    assert _last_loop_number(tmp_path) == 0


def test_last_loop_number_only_seed(tmp_path):
    (tmp_path / "STATS.jsonl").write_text(
        json.dumps({"phase": "seed", "wall_ms": 100}) + "\n"
    )
    assert _last_loop_number(tmp_path) == 0


def test_last_loop_number_multiple_loops(tmp_path):
    lines = [
        json.dumps({"phase": "seed", "wall_ms": 100}),
        json.dumps({"phase": "loop-1", "wall_ms": 200}),
        json.dumps({"phase": "loop-2", "wall_ms": 300}),
        json.dumps({"phase": "loop-3", "wall_ms": 400}),
    ]
    (tmp_path / "STATS.jsonl").write_text("\n".join(lines) + "\n")
    assert _last_loop_number(tmp_path) == 3


def test_last_loop_number_skips_malformed_lines(tmp_path):
    lines = [
        json.dumps({"phase": "loop-1", "wall_ms": 100}),
        "not valid json",
        json.dumps({"phase": "loop-2", "wall_ms": 200}),
    ]
    (tmp_path / "STATS.jsonl").write_text("\n".join(lines) + "\n")
    assert _last_loop_number(tmp_path) == 2


def test_last_loop_number_non_numeric_suffix(tmp_path):
    lines = [
        json.dumps({"phase": "loop-abc", "wall_ms": 100}),
        json.dumps({"phase": "loop-2", "wall_ms": 200}),
    ]
    (tmp_path / "STATS.jsonl").write_text("\n".join(lines) + "\n")
    assert _last_loop_number(tmp_path) == 2


# --- End-to-end roundtrip sentinel ---


def _make_proc(lines: list[str], returncode: int = 0) -> MagicMock:
    """Return a mock Popen whose stdout yields *lines* as NDJSON."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter(l + "\n" for l in lines)
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
    msg_start_1 = _stream_event({
        "type": "message_start",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": {
                "input_tokens": 5,
                "cache_creation_input_tokens": 6663,
                "cache_read_input_tokens": 15771,
            },
        },
    })
    cbs_1 = _stream_event({"type": "content_block_start"})
    md_1 = _stream_event({"type": "message_delta"})

    msg_start_2 = _stream_event({
        "type": "message_start",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 5000,
            },
        },
    })
    cbs_2 = _stream_event({"type": "content_block_start"})
    md_2 = _stream_event({"type": "message_delta"})

    result = json.dumps({
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
    })

    return [
        json.dumps({"type": "system"}),
        msg_start_1, cbs_1, md_1,
        msg_start_2, cbs_2, md_2,
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
    clock = iter([
        0.0,    # turn 1 message_start  → turn_start
        0.100,  # turn 1 content_block_start → token_start (ttft=100ms)
        0.300,  # turn 1 message_delta (decode=200ms)
        0.500,  # turn 2 message_start  → turn_start
        0.650,  # turn 2 content_block_start → token_start (ttft=150ms)
        0.900,  # turn 2 message_delta (decode=250ms)
    ])

    # Step 1: Run _stream() on a mocked CC process with faked time.
    proc = _make_proc(_cc_stream_lines())
    agent = ClaudeCodeAgent()
    with patch("ola.agents.claude_code.time") as mock_time:
        mock_time.monotonic = lambda: next(clock)
        response = agent._stream(proc, "test prompt")
    stats = response.stats

    # Step 2: Write via _append_stats.
    _append_stats(
        tmp_path, "loop-1", stats, wall_ms=10000,
        agent=agent, tasks_before=(0, 3), tasks_after=(1, 3),
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


# --- Stagnation backstop tests ---


class _StagnantAgent(Agent):
    """Agent that always succeeds but never completes any tasks."""

    mnemonic = "cc"
    call_count = 0

    def run(self, prompt, workdir, state_dir=None, labels=None):
        self.call_count += 1
        return AgentResponse(output="All done!", success=True, stats=IterationStats())

    def version(self):
        return "1.0.0"


def test_stagnation_backstop_breaks_at_max(tmp_path, caplog):
    """Loop breaks after _MAX_STAGNANT_LOOPS iterations with zero task progress."""
    folder = tmp_path / "phase"
    folder.mkdir()
    # PLAN.md with a task that never gets checked off (simulates parser bug)
    (folder / "PLAN.md").write_text("- [ ] A task that never completes\n")
    (folder / "LOOP-PROMPT.md").write_text("Do the task.\n")

    agent = _StagnantAgent()

    with (
        caplog.at_level(logging.WARNING, logger="ola.loop"),
        patch("ola.loop._git_commit"),
    ):
        _process_folder(agent, folder, limit=None, agent_root=tmp_path)

    # Agent was called exactly _MAX_STAGNANT_LOOPS times before the loop broke
    assert agent.call_count == _MAX_STAGNANT_LOOPS

    # Warning was logged
    assert any(
        "No task progress" in rec.message and str(_MAX_STAGNANT_LOOPS) in rec.message
        for rec in caplog.records
    )

    # STATS.jsonl has exactly _MAX_STAGNANT_LOOPS rows
    recs = _read_records(folder)
    assert len(recs) == _MAX_STAGNANT_LOOPS


def test_stagnation_resets_on_progress(tmp_path):
    """Stagnation counter resets when the agent makes task progress."""
    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "LOOP-PROMPT.md").write_text("Do the task.\n")

    # Start with 2 unchecked tasks; agent completes one on call 4
    plan_states = []
    for i in range(_MAX_STAGNANT_LOOPS + 2):
        if i < _MAX_STAGNANT_LOOPS - 2:
            plan_states.append("- [ ] Task A\n- [ ] Task B\n")
        elif i == _MAX_STAGNANT_LOOPS - 2:
            # Agent "completes" task A on this iteration
            plan_states.append("- [x] Task A\n- [ ] Task B\n")
        else:
            plan_states.append("- [x] Task A\n- [ ] Task B\n")

    call_idx = 0

    class _ProgressAgent(Agent):
        mnemonic = "cc"

        def run(self, prompt, workdir, state_dir=None, labels=None):
            nonlocal call_idx
            # Write the next plan state BEFORE returning so count_tasks sees it
            (folder / "PLAN.md").write_text(plan_states[min(call_idx, len(plan_states) - 1)])
            call_idx += 1
            return AgentResponse(output="ok", success=True, stats=IterationStats())

        def version(self):
            return "1.0.0"

    # Initial plan state
    (folder / "PLAN.md").write_text("- [ ] Task A\n- [ ] Task B\n")

    agent = _ProgressAgent()
    # Set limit high enough to observe the reset but still terminate
    with patch("ola.loop._git_commit"):
        _process_folder(agent, folder, limit=_MAX_STAGNANT_LOOPS + 2, agent_root=tmp_path)

    # The agent ran more than _MAX_STAGNANT_LOOPS times because progress reset the counter
    assert call_idx > _MAX_STAGNANT_LOOPS


# --- Rate-limit sleep-and-resume tests ---


class _RateLimitAgent(Agent):
    """Agent that returns rate_limited on configurable iterations."""

    mnemonic = "cc"

    def __init__(self, rate_limit_iterations, resets_at):
        self.rate_limit_iterations = set(rate_limit_iterations)
        self.resets_at = resets_at
        self.call_count = 0

    def run(self, prompt, workdir, state_dir=None, labels=None):
        self.call_count += 1
        if self.call_count in self.rate_limit_iterations:
            stats = IterationStats(
                error_type="rate_limited",
                error_message=f"five_hour limit hit, resets at {self.resets_at}",
                rate_limit_resets_at=self.resets_at,
            )
            return AgentResponse(output="Rate limited", success=False, stats=stats)
        stats = IterationStats()
        return AgentResponse(output="ok", success=True, stats=stats)

    def version(self):
        return "1.0.0"


def test_rate_limit_sleep_and_resume(tmp_path, caplog):
    """Rate-limited iteration sleeps and resumes; loop does NOT break."""
    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "LOOP-PROMPT.md").write_text("Do the task.\n")
    # Two tasks: agent "completes" one on call 2 (after waking from sleep)
    (folder / "PLAN.md").write_text("- [ ] Task A\n- [ ] Task B\n")

    resets_at = int(time.time()) + 3
    agent = _RateLimitAgent(rate_limit_iterations={1}, resets_at=resets_at)

    call_idx = [0]
    original_run = agent.run

    def tracking_run(prompt, workdir, state_dir=None, labels=None):
        result = original_run(prompt, workdir, state_dir=state_dir, labels=labels)
        call_idx[0] += 1
        # On second real call, complete task A
        if agent.call_count == 2:
            (folder / "PLAN.md").write_text("- [x] Task A\n- [x] Task B\n")
        return result

    agent.run = tracking_run

    sleep_durations = []
    real_time = time.time

    with (
        caplog.at_level(logging.WARNING, logger="ola.loop"),
        patch("ola.loop._git_commit"),
        patch("ola.loop.time.sleep", side_effect=lambda d: sleep_durations.append(d)),
        patch("ola.loop.time.time", side_effect=real_time),
    ):
        _process_folder(agent, folder, limit=5, agent_root=tmp_path)

    # Agent was called twice: once rate-limited, once successful
    assert agent.call_count == 2

    # Sleep was called with roughly resets_at - now + 10s buffer
    assert len(sleep_durations) == 1
    assert 3 <= sleep_durations[0] <= 15

    # STATS.jsonl has 2 rows: rate_limited + clean
    recs = _read_records(folder)
    assert len(recs) == 2
    assert recs[0]["error_type"] == "rate_limited"
    assert recs[1]["error_type"] is None


def test_rate_limit_cap_exceeds_stops_loop(tmp_path, caplog):
    """Loop breaks when rate limit reset is too far in the future."""
    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "LOOP-PROMPT.md").write_text("Do the task.\n")
    (folder / "PLAN.md").write_text("- [ ] Task A\n")

    resets_at = int(time.time()) + 9 * 3600  # 9 hours, above 8h cap
    agent = _RateLimitAgent(rate_limit_iterations={1}, resets_at=resets_at)

    with (
        caplog.at_level(logging.ERROR, logger="ola.loop"),
        patch("ola.loop._git_commit"),
    ):
        _process_folder(agent, folder, limit=5, agent_root=tmp_path)

    # Agent was called once then loop broke
    assert agent.call_count == 1

    # Error was logged
    assert any(
        "Rate limit reset too far away" in rec.message for rec in caplog.records
    )

    # No sleep was called (loop broke before sleeping)
    recs = _read_records(folder)
    assert len(recs) == 1
    assert recs[0]["error_type"] == "rate_limited"


def test_rate_limit_sleep_interrupted_by_user(tmp_path):
    """KeyboardInterrupt during rate-limit sleep propagates cleanly."""
    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "LOOP-PROMPT.md").write_text("Do the task.\n")
    (folder / "PLAN.md").write_text("- [ ] Task A\n")

    resets_at = int(time.time()) + 60
    agent = _RateLimitAgent(rate_limit_iterations={1}, resets_at=resets_at)

    real_time = time.time

    with (
        patch("ola.loop._git_commit"),
        patch("ola.loop.time.sleep", side_effect=KeyboardInterrupt),
        patch("ola.loop.time.time", side_effect=real_time),
        pytest.raises(KeyboardInterrupt),
    ):
        _process_folder(agent, folder, limit=5, agent_root=tmp_path)

    # Agent was called once before the interrupt
    assert agent.call_count == 1


# --- KeyboardInterrupt stats recording test ---


class _InterruptingAgent(Agent):
    """Agent that raises KeyboardInterrupt on the first call."""

    mnemonic = "cc"
    call_count = 0

    def run(self, prompt, workdir, state_dir=None, labels=None):
        self.call_count += 1
        raise KeyboardInterrupt

    def version(self):
        return "1.0.0"


def test_keyboard_interrupt_writes_stats_row(tmp_path, caplog):
    """SIGINT mid-iteration writes a final STATS row with error_type='interrupted'."""
    folder = tmp_path / "phase"
    folder.mkdir()
    (folder / "LOOP-PROMPT.md").write_text("Do the task.\n")
    (folder / "PLAN.md").write_text("- [ ] Task A\n")

    agent = _InterruptingAgent()

    with (
        caplog.at_level(logging.INFO, logger="ola.loop"),
        patch("ola.loop._git_commit"),
        pytest.raises(KeyboardInterrupt),
    ):
        _process_folder(agent, folder, limit=5, agent_root=tmp_path)

    # Agent was called once before the interrupt
    assert agent.call_count == 1

    # A STATS row was written with error_type="interrupted"
    recs = _read_records(folder)
    assert len(recs) == 1
    assert recs[0]["error_type"] == "interrupted"
    assert recs[0]["error_message"] == "KeyboardInterrupt during iteration"
    assert recs[0]["phase"] == "loop-1"
    assert recs[0]["wall_ms"] >= 0

    # Info log was emitted
    assert any(
        "Interrupted during iteration" in rec.message and "Stats row written" in rec.message
        for rec in caplog.records
    )
