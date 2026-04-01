"""Tests for loop helpers."""

import json

from ola.loop import _last_loop_number


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
