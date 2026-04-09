"""Tests for loop helpers."""

import json

from ola.agents.base import Agent, AgentResponse
from ola.loop import _append_stats, _last_loop_number
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
