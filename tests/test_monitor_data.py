"""Tests for the ola-top monitor data layer."""

from pathlib import Path

from ola.monitor.data import (
    FolderStatus,
    IterationStatus,
    parse_stats_jsonl,
    parse_task_counts,
    read_agent_folder,
    read_folder_status,
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
{"phase": "seed", "wall_ms": 1000, "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 80, "cache_creation_tokens": 20, "num_turns": 3}
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


def test_parse_stats_jsonl():
    iterations = parse_stats_jsonl(SAMPLE_STATS)
    assert len(iterations) == 2
    assert iterations[0].phase == "seed"
    assert iterations[0].wall_ms == 1000
    assert iterations[0].input_tokens == 100
    assert iterations[0].output_tokens == 50
    assert iterations[0].cache_read_tokens == 80
    assert iterations[1].phase == "loop-1"
    assert iterations[1].num_turns == 5


def test_parse_stats_jsonl_with_agent():
    line = (
        '{"phase": "seed", "wall_ms": 500, "input_tokens": 10, "output_tokens": 5,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1,'
        ' "agent": "cc", "agent_version": "1.2.3"}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].agent == "cc"
    assert iterations[0].agent_version == "1.2.3"
    assert iterations[0].agent_display == "Claude Code 1.2.3"


def test_agent_display_no_version():
    it = IterationStatus(phase="seed", agent="oh")
    assert it.agent_display == "OpenHands"


def test_agent_display_empty():
    it = IterationStatus(phase="seed")
    assert it.agent_display == ""


def test_folder_agent_display():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="seed", agent="cc", agent_version="1.0"),
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
    it = IterationStatus(phase="seed", input_tokens=100, cache_read_tokens=80)
    assert it.cache_hit_rate == 80 / 100 * 100


def test_iteration_cache_hit_rate_zero():
    it = IterationStatus(phase="seed", input_tokens=0, cache_read_tokens=0)
    assert it.cache_hit_rate == 0.0


def test_folder_status_aggregation():
    fs = FolderStatus(
        name="test",
        tasks_completed=3,
        tasks_total=5,
        iterations=[
            IterationStatus(
                phase="seed",
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
        '{"phase": "seed", "wall_ms": 500, "input_tokens": 10, "output_tokens": 5, '
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
        '{"phase": "seed", "wall_ms": 10000, "input_tokens": 100, "output_tokens": 50,'
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
    it = IterationStatus(phase="seed", input_tokens=400, output_tokens=100)
    assert it.io_ratio == 4.0


def test_iteration_io_ratio_zero_output():
    it = IterationStatus(phase="seed", input_tokens=100, output_tokens=0)
    assert it.io_ratio == 0.0


def test_iteration_time_breakdown():
    it = IterationStatus(phase="seed", wall_ms=10000, tool_ms=3000)
    llm, tool = it.time_breakdown
    assert tool == 30.0
    assert llm == 70.0


def test_iteration_time_breakdown_zero():
    it = IterationStatus(phase="seed", wall_ms=0, tool_ms=0)
    assert it.time_breakdown == (0.0, 0.0)


def test_folder_total_tool_ms():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="seed", tool_ms=1000),
            IterationStatus(phase="loop-1", tool_ms=2000),
        ],
    )
    assert fs.total_tool_ms == 3000


def test_folder_io_ratio():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="seed", input_tokens=200, output_tokens=50),
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
            IterationStatus(phase="seed", wall_ms=5000, tool_ms=2000),
            IterationStatus(phase="loop-1", wall_ms=5000, tool_ms=1000),
        ],
    )
    llm, tool = fs.time_breakdown
    assert tool == 30.0
    assert llm == 70.0


def test_iteration_llm_tok_per_sec():
    # 500 output tokens, 10s wall, 4s tool → 6s decode → 500/6 ≈ 83.3
    it = IterationStatus(phase="seed", output_tokens=500, wall_ms=10000, tool_ms=4000)
    assert abs(it.llm_tok_per_sec - 500 / 6) < 0.1


def test_iteration_llm_tok_per_sec_no_tool():
    # No tool time → all wall is LLM → 100/10 = 10.0
    it = IterationStatus(phase="seed", output_tokens=100, wall_ms=10000, tool_ms=0)
    assert it.llm_tok_per_sec == 10.0


def test_iteration_llm_tok_per_sec_zero_wall():
    it = IterationStatus(phase="seed", output_tokens=100, wall_ms=0)
    assert it.llm_tok_per_sec == 0.0


def test_folder_llm_tok_per_sec():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(
                phase="seed", output_tokens=200, wall_ms=5000, tool_ms=2000
            ),
            IterationStatus(
                phase="loop-1", output_tokens=300, wall_ms=5000, tool_ms=1000
            ),
        ],
    )
    # total output=500, total wall=10000, total tool=3000, decode=7000ms=7s
    assert abs(fs.llm_tok_per_sec - 500 / 7) < 0.1


def test_iteration_avg_input_tokens():
    it = IterationStatus(phase="seed", input_tokens=9000, num_turns=3)
    assert it.avg_input_tokens == 3000


def test_iteration_avg_input_tokens_zero_turns():
    it = IterationStatus(phase="seed", input_tokens=9000, num_turns=0)
    assert it.avg_input_tokens == 0


def test_folder_avg_input_tokens():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="seed", input_tokens=10000, num_turns=2),
            IterationStatus(phase="loop-1", input_tokens=30000, num_turns=3),
        ],
    )
    # total input=40000, total turns=5 → avg=8000
    assert fs.avg_input_tokens == 8000


def test_folder_max_input_tokens():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="seed", max_input_tokens=15000),
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
        phase="seed", output_tokens=500, wall_ms=10000, tool_ms=4000, ttft_ms=1000
    )
    assert it.llm_tok_per_sec == 100.0


def test_iteration_llm_tok_per_sec_all_ttft():
    # Edge case: decode_ms would be zero or negative → returns 0.0
    it = IterationStatus(
        phase="seed", output_tokens=500, wall_ms=5000, tool_ms=3000, ttft_ms=2000
    )
    assert it.llm_tok_per_sec == 0.0


def test_folder_total_ttft_ms():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(phase="seed", ttft_ms=500),
            IterationStatus(phase="loop-1", ttft_ms=300),
        ],
    )
    assert fs.total_ttft_ms == 800


def test_folder_llm_tok_per_sec_with_ttft():
    fs = FolderStatus(
        name="test",
        iterations=[
            IterationStatus(
                phase="seed",
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
    # total output=500, total wall=10000, total tool=2000, total ttft=1000
    # decode = 10000 - 2000 - 1000 = 7000ms = 7s → 500/7 ≈ 71.4
    assert abs(fs.llm_tok_per_sec - 500 / 7) < 0.1


def test_parse_stats_jsonl_with_ttft():
    line = (
        '{"phase": "seed", "wall_ms": 5000, "input_tokens": 100, "output_tokens": 50,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1,'
        ' "tool_ms": 1000, "ttft_ms": 800}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].ttft_ms == 800


def test_parse_stats_jsonl_backward_compat_ttft():
    """Old STATS.jsonl without ttft_ms field defaults to 0."""
    line = (
        '{"phase": "seed", "wall_ms": 5000, "input_tokens": 100, "output_tokens": 50,'
        ' "cache_read_tokens": 0, "cache_creation_tokens": 0, "num_turns": 1}\n'
    )
    iterations = parse_stats_jsonl(line)
    assert iterations[0].ttft_ms == 0
