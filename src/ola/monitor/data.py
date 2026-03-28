"""Data layer for the ola-top monitor: parse agent folders into status models."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


_AGENT_FULL_NAMES: dict[str, str] = {
    "cc": "Claude Code",
    "oh": "OpenHands",
}


@dataclass
class IterationStatus:
    """Stats for a single iteration (seed or loop-N)."""

    phase: str
    wall_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    num_turns: int = 0
    agent: str = ""
    agent_version: str = ""
    models: list[str] = field(default_factory=list)
    tool_ms: int = 0
    tasks_completed: int = 0
    tasks_total: int = 0
    tasks_completed_delta: int = 0

    @property
    def agent_display(self) -> str:
        """Full agent name with version, e.g. 'Claude Code 1.2.3'."""
        name = _AGENT_FULL_NAMES.get(self.agent, self.agent)
        if self.agent_version:
            return f"{name} {self.agent_version}"
        return name

    @property
    def cache_hit_rate(self) -> float:
        """Cache hit rate as a percentage (0-100)."""
        total = self.input_tokens + self.cache_read_tokens
        if total == 0:
            return 0.0
        return self.cache_read_tokens / total * 100

    @property
    def io_ratio(self) -> float:
        """Input/output token ratio."""
        if self.output_tokens == 0:
            return 0.0
        return self.input_tokens / self.output_tokens

    @property
    def time_breakdown(self) -> tuple[float, float]:
        """(llm_pct, tool_pct) as percentages of wall time."""
        if self.wall_ms == 0:
            return (0.0, 0.0)
        tool_pct = self.tool_ms / self.wall_ms * 100
        llm_pct = 100.0 - tool_pct
        return (llm_pct, tool_pct)

    @property
    def llm_tok_per_sec(self) -> float:
        """Output tokens per second during the LLM phase (excluding tool time)."""
        llm_ms = self.wall_ms - self.tool_ms
        if llm_ms <= 0:
            return 0.0
        return self.output_tokens / (llm_ms / 1000)


@dataclass
class FolderStatus:
    """Aggregated status for one agent subfolder."""

    name: str
    tasks_completed: int = 0
    tasks_total: int = 0
    iterations: list[IterationStatus] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        return sum(it.input_tokens for it in self.iterations)

    @property
    def total_output_tokens(self) -> int:
        return sum(it.output_tokens for it in self.iterations)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(it.cache_read_tokens for it in self.iterations)

    @property
    def total_cache_creation_tokens(self) -> int:
        return sum(it.cache_creation_tokens for it in self.iterations)

    @property
    def total_wall_ms(self) -> int:
        return sum(it.wall_ms for it in self.iterations)

    @property
    def cache_hit_rate(self) -> float:
        """Aggregate cache hit rate as a percentage (0-100)."""
        total = self.total_input_tokens + self.total_cache_read_tokens
        if total == 0:
            return 0.0
        return self.total_cache_read_tokens / total * 100

    @property
    def agent_display(self) -> str:
        """Agent display from the most recent iteration."""
        if self.iterations:
            return self.iterations[-1].agent_display
        return ""

    @property
    def total_tool_ms(self) -> int:
        return sum(it.tool_ms for it in self.iterations)

    @property
    def io_ratio(self) -> float:
        """Input/output token ratio."""
        if self.total_output_tokens == 0:
            return 0.0
        return self.total_input_tokens / self.total_output_tokens

    @property
    def time_breakdown(self) -> tuple[float, float]:
        """(llm_pct, tool_pct) as percentages of wall time."""
        wall = self.total_wall_ms
        if wall == 0:
            return (0.0, 0.0)
        tool_pct = self.total_tool_ms / wall * 100
        llm_pct = 100.0 - tool_pct
        return (llm_pct, tool_pct)

    @property
    def llm_tok_per_sec(self) -> float:
        """Aggregate output tokens per second during LLM phases."""
        llm_ms = self.total_wall_ms - self.total_tool_ms
        if llm_ms <= 0:
            return 0.0
        return self.total_output_tokens / (llm_ms / 1000)

    @property
    def model_display(self) -> str:
        """Unique model names across all iterations, comma-separated."""
        seen: list[str] = []
        for it in self.iterations:
            for m in it.models:
                if m and m not in seen:
                    seen.append(m)
        return ", ".join(seen)


def parse_task_counts(plan_text: str) -> tuple[int, int]:
    """Parse PLAN.md text and return (completed, total) task counts."""
    completed = len(re.findall(r"- \[x\]", plan_text, re.IGNORECASE))
    unchecked = len(re.findall(r"- \[ \]", plan_text))
    return completed, completed + unchecked


def parse_stats_jsonl(stats_text: str) -> list[IterationStatus]:
    """Parse STATS.jsonl text into a list of IterationStatus objects."""
    iterations: list[IterationStatus] = []
    for line in stats_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        iterations.append(
            IterationStatus(
                phase=record["phase"],
                wall_ms=record.get("wall_ms", 0),
                input_tokens=record.get("input_tokens", 0),
                output_tokens=record.get("output_tokens", 0),
                cache_read_tokens=record.get("cache_read_tokens", 0),
                cache_creation_tokens=record.get("cache_creation_tokens", 0),
                num_turns=record.get("num_turns", 0),
                agent=record.get("agent", ""),
                agent_version=record.get("agent_version", ""),
                models=record.get("models", []),
                tool_ms=record.get("tool_ms", 0),
                tasks_completed=record.get("tasks_completed", 0),
                tasks_total=record.get("tasks_total", 0),
                tasks_completed_delta=record.get("tasks_completed_delta", 0),
            )
        )
    return iterations


def read_folder_status(folder: Path) -> FolderStatus:
    """Read a single agent subfolder and return its FolderStatus."""
    status = FolderStatus(name=folder.name)

    plan_file = folder / "PLAN.md"
    if plan_file.exists():
        status.tasks_completed, status.tasks_total = parse_task_counts(
            plan_file.read_text()
        )

    stats_file = folder / "STATS.jsonl"
    if stats_file.exists():
        status.iterations = parse_stats_jsonl(stats_file.read_text())

    return status


def read_agent_folder(agent_path: Path) -> list[FolderStatus]:
    """Read all subfolders of an agent directory and return their statuses.

    Subfolders are sorted by name. Hidden directories (starting with .) are skipped.
    """
    if not agent_path.is_dir():
        return []
    subfolders = sorted(
        p for p in agent_path.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    return [read_folder_status(f) for f in subfolders]
