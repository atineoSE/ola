"""Tests for the ola-top monitor data layer."""

import json
from pathlib import Path

from ola.monitor.data import (
    FolderStatus,
    IterationStatus,
    TaskRow,
    parse_stats_jsonl,
    parse_task_counts,
    read_agent_folder,
    read_folder_status,
    read_task_rows,
)

SAMPLE_PLAN = """\
# My Plan

## Section A

- [x] Task one
- [x] Task two
- [ ] Task three

## Section B

- [ ] Task four
- [x] Task five
"""

SAMPLE_STATS = """\
{"phase": "task-t1-1", "wall_ms": 1000, "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 80, "cache_creation_tokens": 20, "num_turns": 3}
{"phase": "loop-1", "wall_ms": 2000, "input_tokens": 200, "output_tokens": 100, "cache_read_tokens": 150, "cache_creation_tokens": 50, "num_turns": 5}
"""


def test_parse_task_counts():
    completed, total = parse_task_counts(SAMPLE_PLAN)
    assert completed == 3
    assert total == 5


def test_parse_task_counts_empty():
    completed, total = parse_task_counts("")
    assert completed == 0
    assert total == 0


def test_parse_task_counts_all_done():
    text = "- [x] A\n- [x] B\n"
    completed, total = parse_task_counts(text)
    assert completed == 2
    assert total == 2


def test_parse_task_counts_ignores_code_block():
    """Cross-module guard: monitor's parse_task_counts delegates to plan.py
    and correctly skips fenced code blocks."""
    text = "- [x] Real task\n```bash\necho '- [ ] fake'\n```\n"
    completed, total = parse_task_counts(text)
    assert completed == 1
    assert total == 1


def test_parse_stats_jsonl():
    iterations = parse_stats_jsonl(SAMPLE_STATS)
    assert len(iterations) == 2
    assert iterations[0].phase == "task-t1-1"
    assert iterations[0].wall_ms == 1000
    assert iterations[0].input_tokens == 100
    assert iterations[0].output_tokens == 50
    assert iterations[0].cache_read_tokens == 80
    assert iterations[1].phase == "loop-1"
    assert iterations[1].num_turns == 5


def test_parse_stats_jsonl_mixed_legacy_and_parallel_phases():
    """A folder with a legacy (loop-N) and new (task-<id>-<n>) rows all parse.

    The parser treats ``phase`` as an opaque string, so the parallel-mode shape
    coexists with the legacy shapes without any parser branching.
    """
    mixed = (
        '{"phase": "task-t1-1", "wall_ms": 1000, "input_tokens": 100, "output_tokens": 50}\n'
        '{"phase": "loop-1", "wall_ms": 2000, "input_tokens": 200, "output_tokens": 99}\n'
        '{"phase": "task-t-abc1234-1", "wall_ms": 1500, "input_tokens": 300,'
        ' "output_tokens": 120, "tasks_completed_delta": 1}\n'
        '{"phase": "task-t-def5678-2", "wall_ms": 1700, "input_tokens": 400,'
        ' "output_tokens": 130}\n'
    )
    iterations = parse_stats_jsonl(mixed)
    assert [it.phase for it in iterations] == [
        "task-t1-1",
        "loop-1",
        "task-t-abc1234-1",
        "task-t-def5678-2",
    ]
    assert iterations[2].tasks_completed_delta == 1
    assert iterations[3].input_tokens == 400


def test_parse_stats_jsonl_with_agent():
    line = (
        '{"phase": "task-t1-1", "wall_ms": 500, "input_tokens": 10, "output_tokens": 5,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1,'
        ' "agent": "cc", "agent_version": "1.2.3"}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].agent == "cc"
    assert iterations[0].agent_version == "1.2.3"
    assert iterations[0].agent_display == "Claude Code 1.2.3"


def test_agent_display_no_version():
    it = IterationStatus(phase="task-t1-1", agent="oh")
    assert it.agent_display == "OpenHands"


def test_agent_display_codex():
    it = IterationStatus(phase="task-t1-1", agent="cx", agent_version="0.1.0")
    assert it.agent_display == "Codex 0.1.0"


def test_agent_display_codex_no_version():
    it = IterationStatus(phase="task-t1-1", agent="cx")
    assert it.agent_display == "Codex"


def test_agent_display_empty():
    it = IterationStatus(phase="task-t1-1")
    assert it.agent_display == ""


def test_folder_agent_display():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", agent="cc", agent_version="1.0"),
            IterationStatus(phase="loop-1", agent="cc", agent_version="1.1"),
        ],
    )
    assert fs.agent_display == "Claude Code 1.1"


def test_folder_agent_display_empty():
    fs = FolderStatus(name="test")
    assert fs.agent_display == ""


def test_parse_stats_jsonl_empty():
    assert parse_stats_jsonl("") == []
    assert parse_stats_jsonl("  \n  ") == []


def test_iteration_cache_hit_rate():
    # input_tokens already includes cache_read_tokens (as stored by both agents)
    it = IterationStatus(phase="task-t1-1", input_tokens=100, cache_read_tokens=80)
    assert it.cache_hit_rate == 80 / 100 * 100


def test_iteration_cache_hit_rate_zero():
    it = IterationStatus(phase="task-t1-1", input_tokens=0, cache_read_tokens=0)
    assert it.cache_hit_rate == 0.0


def test_folder_status_aggregation():
    fs = FolderStatus(
        name="test",
        tasks_completed=3,
        tasks_total=5,
        iterations=[
            IterationStatus(
                phase="task-t1-1",
                wall_ms=1000,
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=80,
                cache_creation_tokens=20,
            ),
            IterationStatus(
                phase="loop-1",
                wall_ms=2000,
                input_tokens=200,
                output_tokens=100,
                cache_read_tokens=150,
                cache_creation_tokens=50,
            ),
        ],
    )
    assert fs.total_input_tokens == 300
    assert fs.total_output_tokens == 150
    assert fs.total_cache_read_tokens == 230
    assert fs.total_cache_creation_tokens == 70
    assert fs.total_wall_ms == 3000
    expected_rate = 230 / 300 * 100
    assert abs(fs.cache_hit_rate - expected_rate) < 0.01


def test_folder_status_empty():
    fs = FolderStatus(name="empty")
    assert fs.total_input_tokens == 0
    assert fs.cache_hit_rate == 0.0


def test_read_folder_status(tmp_path: Path):
    folder = tmp_path / "01-task"
    folder.mkdir()
    (folder / "PLAN.md").write_text(SAMPLE_PLAN)
    (folder / "STATS.jsonl").write_text(SAMPLE_STATS)

    status = read_folder_status(folder)
    assert status.name == "01-task"
    assert status.tasks_completed == 3
    assert status.tasks_total == 5
    assert len(status.iterations) == 2


def test_read_folder_status_missing_files(tmp_path: Path):
    folder = tmp_path / "02-empty"
    folder.mkdir()

    status = read_folder_status(folder)
    assert status.tasks_completed == 0
    assert status.tasks_total == 0
    assert status.iterations == []


def test_read_agent_folder(tmp_path: Path):
    # Create two subfolders
    f1 = tmp_path / "01-first"
    f1.mkdir()
    (f1 / "PLAN.md").write_text("- [x] Done\n- [ ] Todo\n")
    (f1 / "STATS.jsonl").write_text(
        '{"phase": "task-t1-1", "wall_ms": 500, "input_tokens": 10, "output_tokens": 5, '
        '"cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1}\n'
    )

    f2 = tmp_path / "02-second"
    f2.mkdir()
    (f2 / "PLAN.md").write_text("- [ ] A\n- [ ] B\n")

    # Hidden dir should be skipped
    hidden = tmp_path / ".hidden"
    hidden.mkdir()

    statuses = read_agent_folder(tmp_path)
    assert len(statuses) == 2
    assert statuses[0].name == "01-first"
    assert statuses[0].tasks_completed == 1
    assert statuses[0].tasks_total == 2
    assert statuses[1].name == "02-second"
    assert statuses[1].tasks_total == 2
    assert statuses[1].tasks_completed == 0


def test_read_agent_folder_nonexistent(tmp_path: Path):
    result = read_agent_folder(tmp_path / "nonexistent")
    assert result == []


def test_parse_stats_jsonl_with_tool_ms():
    line = (
        '{"phase": "task-t1-1", "wall_ms": 10000, "input_tokens": 100, "output_tokens": 50,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1,'
        ' "tool_ms": 4000}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].tool_ms == 4000


def test_parse_stats_jsonl_with_task_fields():
    line = (
        '{"phase": "loop-1", "wall_ms": 5000, "input_tokens": 100, "output_tokens": 50,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1,'
        ' "tasks_completed": 3, "tasks_total": 5, "tasks_completed_delta": 2}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].tasks_completed == 3
    assert iterations[0].tasks_total == 5
    assert iterations[0].tasks_completed_delta == 2


def test_iteration_io_ratio():
    it = IterationStatus(phase="task-t1-1", input_tokens=400, output_tokens=100)
    assert it.io_ratio == 4.0


def test_iteration_io_ratio_zero_output():
    it = IterationStatus(phase="task-t1-1", input_tokens=100, output_tokens=0)
    assert it.io_ratio == 0.0


def test_iteration_time_breakdown():
    it = IterationStatus(phase="task-t1-1", wall_ms=10000, tool_ms=3000)
    llm, tool = it.time_breakdown
    assert tool == 30.0
    assert llm == 70.0


def test_iteration_time_breakdown_zero():
    it = IterationStatus(phase="task-t1-1", wall_ms=0, tool_ms=0)
    assert it.time_breakdown == (0.0, 0.0)


def test_folder_total_tool_ms():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", tool_ms=1000),
            IterationStatus(phase="loop-1", tool_ms=2000),
        ],
    )
    assert fs.total_tool_ms == 3000


def test_folder_io_ratio():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", input_tokens=200, output_tokens=50),
            IterationStatus(phase="loop-1", input_tokens=300, output_tokens=100),
        ],
    )
    assert fs.io_ratio == 500 / 150


def test_folder_io_ratio_zero_output():
    fs = FolderStatus(name="test")
    assert fs.io_ratio == 0.0


def test_folder_time_breakdown():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", wall_ms=5000, tool_ms=2000),
            IterationStatus(phase="loop-1", wall_ms=5000, tool_ms=1000),
        ],
    )
    llm, tool = fs.time_breakdown
    assert tool == 30.0
    assert llm == 70.0


def test_iteration_llm_tok_per_sec():
    # 500 output tokens, 10s wall, 4s tool → 6s decode → 500/6 ≈ 83.3
    it = IterationStatus(
        phase="task-t1-1", output_tokens=500, wall_ms=10000, tool_ms=4000
    )
    assert abs(it.llm_tok_per_sec - 500 / 6) < 0.1


def test_iteration_llm_tok_per_sec_no_tool():
    # No tool time → all wall is LLM → 100/10 = 10.0
    it = IterationStatus(phase="task-t1-1", output_tokens=100, wall_ms=10000, tool_ms=0)
    assert it.llm_tok_per_sec == 10.0


def test_iteration_llm_tok_per_sec_zero_wall():
    it = IterationStatus(phase="task-t1-1", output_tokens=100, wall_ms=0)
    assert it.llm_tok_per_sec == 0.0


def test_folder_llm_tok_per_sec():
    # task-t1-1: decode = 5000 - 2000 = 3000ms → 200/3 ≈ 66.7
    # loop-1: decode = 5000 - 1000 = 4000ms → 300/4 = 75.0
    # median of [66.7, 75.0] = 70.83
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(
                phase="task-t1-1", output_tokens=200, wall_ms=5000, tool_ms=2000
            ),
            IterationStatus(
                phase="loop-1", output_tokens=300, wall_ms=5000, tool_ms=1000
            ),
        ],
    )
    expected = (200 / 3 + 300 / 4) / 2
    assert abs(fs.llm_tok_per_sec - expected) < 0.1


def test_iteration_avg_input_tokens():
    it = IterationStatus(phase="task-t1-1", input_tokens=9000, num_turns=3)
    assert it.avg_input_tokens == 3000


def test_iteration_avg_input_tokens_zero_turns():
    it = IterationStatus(phase="task-t1-1", input_tokens=9000, num_turns=0)
    assert it.avg_input_tokens == 0


def test_folder_avg_input_tokens():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", input_tokens=10000, num_turns=2),
            IterationStatus(phase="loop-1", input_tokens=30000, num_turns=3),
        ],
    )
    # total input=40000, total turns=5 → avg=8000
    assert fs.avg_input_tokens == 8000


def test_folder_max_input_tokens():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", max_input_tokens=15000),
            IterationStatus(phase="loop-1", max_input_tokens=42000),
            IterationStatus(phase="loop-2", max_input_tokens=38000),
        ],
    )
    assert fs.max_input_tokens == 42000


def test_folder_max_input_tokens_empty():
    fs = FolderStatus(name="test")
    assert fs.max_input_tokens == 0


# --- TTFT tests ---


def test_iteration_llm_tok_per_sec_with_ttft():
    # 500 output tokens, 10s wall, 4s tool, 1s ttft → 5s decode → 100 tok/s
    it = IterationStatus(
        phase="task-t1-1", output_tokens=500, wall_ms=10000, tool_ms=4000, ttft_ms=1000
    )
    assert it.llm_tok_per_sec == 100.0


def test_iteration_llm_tok_per_sec_all_ttft():
    # Edge case: decode_ms would be zero or negative → returns 0.0
    it = IterationStatus(
        phase="task-t1-1", output_tokens=500, wall_ms=5000, tool_ms=3000, ttft_ms=2000
    )
    assert it.llm_tok_per_sec == 0.0


def test_folder_median_ttft_ms():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", ttft_ms=500),
            IterationStatus(phase="loop-1", ttft_ms=300),
            IterationStatus(phase="loop-2", ttft_ms=400),
        ],
    )
    assert fs.median_ttft_ms == 400


def test_folder_llm_tok_per_sec_median():
    # task-t1-1: decode = 5000 - 1000 - 500 = 3500ms → 200/3.5 ≈ 57.1
    # loop-1: decode = 5000 - 1000 - 500 = 3500ms → 300/3.5 ≈ 85.7
    # median of [57.1, 85.7] = 71.4
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(
                phase="task-t1-1",
                output_tokens=200,
                wall_ms=5000,
                tool_ms=1000,
                ttft_ms=500,
            ),
            IterationStatus(
                phase="loop-1",
                output_tokens=300,
                wall_ms=5000,
                tool_ms=1000,
                ttft_ms=500,
            ),
        ],
    )
    expected = (200 / 3.5 + 300 / 3.5) / 2
    assert abs(fs.llm_tok_per_sec - expected) < 0.1


def test_parse_stats_jsonl_with_ttft():
    line = (
        '{"phase": "task-t1-1", "wall_ms": 5000, "input_tokens": 100, "output_tokens": 50,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1,'
        ' "tool_ms": 1000, "ttft_ms": 800}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].ttft_ms == 800


def test_parse_stats_jsonl_backward_compat_ttft():
    """Old STATS.jsonl without ttft_ms field defaults to 0."""
    line = (
        '{"phase": "task-t1-1", "wall_ms": 5000, "input_tokens": 100, "output_tokens": 50,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].ttft_ms == 0


# --- llm_ms field tests ---


def test_parse_stats_jsonl_with_llm_ms():
    """llm_ms field is read correctly."""
    line = (
        '{"phase": "task-t1-1", "wall_ms": 5000, "input_tokens": 100, "output_tokens": 50,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1,'
        ' "llm_ms": 3000}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].llm_ms == 3000


def test_parse_stats_jsonl_backward_compat_llm_ms():
    """Old STATS.jsonl without llm_ms field defaults to 0."""
    line = (
        '{"phase": "task-t1-1", "wall_ms": 5000, "input_tokens": 100, "output_tokens": 50,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].llm_ms == 0


# --- Backward compatibility for other fields ---


def test_parse_stats_jsonl_backward_compat_models():
    """Missing models field defaults to empty list."""
    line = '{"phase": "task-t1-1", "wall_ms": 1000}\n'
    iterations = parse_stats_jsonl(line)
    assert iterations[0].models == []


def test_parse_stats_jsonl_backward_compat_streamed():
    """Missing streamed field defaults to True."""
    line = '{"phase": "task-t1-1", "wall_ms": 1000}\n'
    iterations = parse_stats_jsonl(line)
    assert iterations[0].streamed is True


def test_parse_stats_jsonl_backward_compat_minimal():
    """Minimal record with only phase — all other fields default."""
    line = '{"phase": "task-t1-1", "wall_ms": 500}\n'
    iterations = parse_stats_jsonl(line)
    it = iterations[0]
    assert it.phase == "task-t1-1"
    assert it.wall_ms == 500
    assert it.input_tokens == 0
    assert it.output_tokens == 0
    assert it.cache_read_tokens == 0
    assert it.cache_creation_tokens == 0
    assert it.num_turns == 0
    assert it.models == []
    assert it.tool_ms == 0
    assert it.llm_ms == 0
    assert it.max_input_tokens == 0
    assert it.ttft_ms == 0
    assert it.streamed is True
    assert it.agent == ""
    assert it.agent_version == ""
    assert it.tasks_completed == 0
    assert it.tasks_total == 0
    assert it.tasks_completed_delta == 0


# --- FolderStatus property tests ---


def test_folder_model_display():
    """model_display deduplicates and preserves order across iterations."""
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(
                phase="task-t1-1", models=["claude-3-opus", "claude-3-sonnet"]
            ),
            IterationStatus(phase="loop-1", models=["claude-3-opus"]),
        ],
    )
    assert fs.model_display == "claude-3-opus, claude-3-sonnet"


def test_folder_model_display_empty():
    """No models across any iteration returns empty string."""
    fs = FolderStatus(name="test", iterations=[IterationStatus(phase="task-t1-1")])
    assert fs.model_display == ""


def test_folder_all_streamed_mixed():
    """Mixed streamed flags → all_streamed is False."""
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", streamed=True),
            IterationStatus(phase="loop-1", streamed=False),
        ],
    )
    assert fs.all_streamed is False


def test_folder_all_streamed_all_true():
    """All streamed=True → all_streamed is True."""
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="task-t1-1", streamed=True),
            IterationStatus(phase="loop-1", streamed=True),
        ],
    )
    assert fs.all_streamed is True


# --- Error field tests ---


def test_parse_stats_jsonl_with_error_fields():
    """STATS row with error fields is parsed correctly."""
    line = (
        '{"phase": "loop-3", "wall_ms": 500, "input_tokens": 10, "output_tokens": 0,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1,'
        ' "error_type": "rate_limited", "error_message": "five_hour limit hit",'
        ' "rate_limit_resets_at": 1700000000}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].error_type == "rate_limited"
    assert iterations[0].error_message == "five_hour limit hit"
    assert iterations[0].rate_limit_resets_at == 1700000000


def test_parse_stats_jsonl_backward_compat_error_fields():
    """Old STATS.jsonl without error fields defaults to None."""
    line = '{"phase": "task-t1-1", "wall_ms": 1000}\n'
    iterations = parse_stats_jsonl(line)
    assert iterations[0].error_type is None
    assert iterations[0].error_message is None
    assert iterations[0].rate_limit_resets_at is None


def test_iteration_stats_error_fields_in_model_dump():
    """IterationStats error fields round-trip through model_dump (used by _append_stats)."""
    from ola.stats import IterationStats

    stats = IterationStats(
        error_type="rate_limited",
        error_message="five_hour limit hit, resets at 2024-01-01T00:00:00",
        rate_limit_resets_at=1700000000,
    )
    dumped = stats.model_dump()
    assert dumped["error_type"] == "rate_limited"
    assert dumped["error_message"].startswith("five_hour")
    assert dumped["rate_limit_resets_at"] == 1700000000


def test_iteration_stats_error_fields_default_none():
    """IterationStats error fields default to None."""
    from ola.stats import IterationStats

    stats = IterationStats()
    assert stats.error_type is None
    assert stats.error_message is None
    assert stats.rate_limit_resets_at is None


# --- TaskRow / read_task_rows tests ---


def _write_tasks_json(folder: Path, tasks: list[dict]) -> None:
    ola_dir = folder / ".ola"
    ola_dir.mkdir(parents=True, exist_ok=True)
    (ola_dir / "tasks.json").write_text(json.dumps({"tasks": tasks}))


def _write_events_jsonl(folder: Path, events: list[dict]) -> None:
    ola_dir = folder / ".ola"
    ola_dir.mkdir(parents=True, exist_ok=True)
    (ola_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def test_read_task_rows_no_ola_returns_empty(tmp_path: Path):
    """A folder without .ola/tasks.json is not in parallel mode → no rows."""
    folder = tmp_path / "01-task"
    folder.mkdir()
    (folder / "PLAN.md").write_text("- [ ] A\n")
    assert read_task_rows(folder) == []


def test_read_task_rows_spine_from_tasks_json(tmp_path: Path):
    """Rows follow tasks.json order, carrying status and attempt count."""
    folder = tmp_path / "01-task"
    folder.mkdir()
    _write_tasks_json(
        folder,
        [
            {
                "task_id": "t-aaa",
                "text": "First task",
                "line_no": 1,
                "status": "complete",
                "attempts": 1,
                "last_error": None,
            },
            {
                "task_id": "t-bbb",
                "text": "Second task",
                "line_no": 2,
                "status": "running",
                "attempts": 0,
                "last_error": None,
            },
        ],
    )
    rows = read_task_rows(folder)
    assert [r.task_id for r in rows] == ["t-aaa", "t-bbb"]
    assert rows[0].status == "complete"
    assert rows[0].attempt == 1
    assert rows[1].status == "running"
    # No events → no progress, zero elapsed.
    assert rows[1].elapsed_s == 0.0
    assert rows[1].last_progress_message == ""


def test_read_task_rows_truncates_text(tmp_path: Path):
    folder = tmp_path / "01-task"
    folder.mkdir()
    long_text = "x" * 200
    _write_tasks_json(
        folder,
        [{"task_id": "t-aaa", "text": long_text, "line_no": 1, "status": "pending"}],
    )
    row = read_task_rows(folder)[0]
    assert len(row.text) == 60
    assert row.text.endswith("…")


def test_read_task_rows_folds_in_events(tmp_path: Path):
    """elapsed_s spans first→last event ts; last message is the most recent one."""
    folder = tmp_path / "01-task"
    folder.mkdir()
    _write_tasks_json(
        folder,
        [{"task_id": "t-aaa", "text": "Task", "line_no": 1, "status": "complete"}],
    )
    _write_events_jsonl(
        folder,
        [
            {
                "task_id": "t-aaa",
                "status": "started",
                "ts": "2026-05-27T14:00:00.000Z",
                "data": {},
            },
            {
                "task_id": "t-aaa",
                "status": "working",
                "ts": "2026-05-27T14:00:05.000Z",
                "data": {"message": "running tests"},
            },
            {
                "task_id": "t-aaa",
                "status": "complete",
                "ts": "2026-05-27T14:00:12.000Z",
                "data": {},
            },
        ],
    )
    row = read_task_rows(folder)[0]
    assert row.elapsed_s == 12.0
    assert row.last_progress_message == "running tests"


def test_read_task_rows_skips_malformed_event_lines(tmp_path: Path):
    """A half-written events.jsonl line must not break the monitor."""
    folder = tmp_path / "01-task"
    folder.mkdir()
    _write_tasks_json(
        folder,
        [{"task_id": "t-aaa", "text": "Task", "line_no": 1, "status": "running"}],
    )
    ola_dir = folder / ".ola"
    (ola_dir / "events.jsonl").write_text(
        json.dumps(
            {"task_id": "t-aaa", "status": "started", "ts": "2026-05-27T14:00:00.000Z"}
        )
        + "\n"
        + "{not valid json\n"
        + json.dumps(
            {
                "task_id": "t-aaa",
                "status": "working",
                "ts": "2026-05-27T14:00:03.000Z",
                "data": {"message": "still going"},
            }
        )
        + "\n"
    )
    row = read_task_rows(folder)[0]
    assert row.elapsed_s == 3.0
    assert row.last_progress_message == "still going"


def test_read_task_rows_single_event_zero_elapsed(tmp_path: Path):
    """A task with only a started event has no measurable elapsed span yet."""
    folder = tmp_path / "01-task"
    folder.mkdir()
    _write_tasks_json(
        folder,
        [{"task_id": "t-aaa", "text": "Task", "line_no": 1, "status": "running"}],
    )
    _write_events_jsonl(
        folder,
        [{"task_id": "t-aaa", "status": "started", "ts": "2026-05-27T14:00:00.000Z"}],
    )
    row = read_task_rows(folder)[0]
    assert row.elapsed_s == 0.0


def test_task_row_defaults():
    row = TaskRow(task_id="t-aaa", text="Task", status="pending")
    assert row.attempt == 0
    assert row.elapsed_s == 0.0
    assert row.last_progress_message == ""
    assert row.stats is None


def test_read_task_rows_folds_in_stats_summed_across_attempts(tmp_path: Path):
    """Per-task STATS rows (task-<id>-<attempt>) are summed into TaskRow.stats."""
    folder = tmp_path / "01-task"
    folder.mkdir()
    _write_tasks_json(
        folder,
        [
            {
                "task_id": "t-aaa",
                "text": "A",
                "line_no": 1,
                "status": "complete",
                "attempts": 2,
            },
            {"task_id": "t-bbb", "text": "B", "line_no": 2, "status": "pending"},
        ],
    )
    iterations = parse_stats_jsonl(
        json.dumps({"phase": "seed", "input_tokens": 999})
        + "\n"
        + json.dumps({"phase": "task-t-aaa-1", "input_tokens": 100, "num_turns": 2})
        + "\n"
        + json.dumps({"phase": "task-t-aaa-2", "input_tokens": 50, "num_turns": 3})
        + "\n"
    )
    rows = read_task_rows(folder, iterations)
    # t-aaa sums both attempts; the unrelated "seed" row never leaks in.
    assert rows[0].stats is not None
    assert rows[0].stats.input_tokens == 150
    assert rows[0].stats.num_turns == 5
    # A task with no matching STATS row carries no stats.
    assert rows[1].stats is None


def test_read_task_rows_stats_match_is_prefix_plus_digits(tmp_path: Path):
    """Collision-suffixed ids (t-aaa-2) must not swallow a sibling's rows."""
    folder = tmp_path / "01-task"
    folder.mkdir()
    _write_tasks_json(
        folder,
        [
            {"task_id": "t-aaa", "text": "A", "line_no": 1, "status": "complete"},
            {"task_id": "t-aaa-2", "text": "A2", "line_no": 2, "status": "complete"},
        ],
    )
    iterations = parse_stats_jsonl(
        json.dumps({"phase": "task-t-aaa-1", "input_tokens": 10})
        + "\n"
        + json.dumps({"phase": "task-t-aaa-2-1", "input_tokens": 20})
        + "\n"
    )
    rows = read_task_rows(folder, iterations)
    by_id = {r.task_id: r for r in rows}
    # task-t-aaa-2 is t-aaa attempt 2, NOT a row for the id "t-aaa-2".
    assert by_id["t-aaa"].stats.input_tokens == 10
    assert by_id["t-aaa-2"].stats.input_tokens == 20


def test_read_task_rows_without_iterations_has_no_stats(tmp_path: Path):
    """Called without iterations (back-compat), rows carry stats=None."""
    folder = tmp_path / "01-task"
    folder.mkdir()
    _write_tasks_json(
        folder,
        [{"task_id": "t-aaa", "text": "A", "line_no": 1, "status": "pending"}],
    )
    assert read_task_rows(folder)[0].stats is None
