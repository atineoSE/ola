"""Plan folder discovery and PLAN.md parsing."""

import re
from pathlib import Path

_CHECKBOX_RE = re.compile(r"^[ \t]*[-*+] \[( |x|X)\] ")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")


def _count_checkboxes(text: str) -> tuple[int, int]:
    """Walk *text* line-by-line, skipping fenced code blocks.

    Returns (checked, checked + unchecked).

    Known limitations: indented (4-space) code blocks and setext headings
    are not detected — only backtick and tilde fences.
    """
    checked = 0
    unchecked = 0
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _CHECKBOX_RE.match(line)
        if m:
            if m.group(1) == " ":
                unchecked += 1
            else:
                checked += 1
    return checked, checked + unchecked


def parse_task_counts(text: str) -> tuple[int, int]:
    """Canonical string-in parser: return (completed, total) from markdown text."""
    return _count_checkboxes(text)


def discover_plan_folders(plan_path: Path) -> list[Path]:
    """Return sorted subfolders of the plan path, or empty list if none."""
    if not plan_path.is_dir():
        raise FileNotFoundError(f"Plan path does not exist: {plan_path}")

    subfolders = sorted(
        p for p in plan_path.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    return subfolders


def has_outstanding_tasks(plan_path: Path) -> bool:
    """Check if PLAN.md exists and has unchecked todo items."""
    plan_file = plan_path / "PLAN.md"
    if not plan_file.exists():
        return False
    content = plan_file.read_text()
    completed, total = parse_task_counts(content)
    return total > completed


def count_tasks(folder: Path) -> tuple[int, int]:
    """Read PLAN.md in *folder* and return (completed, total) task counts."""
    plan_file = folder / "PLAN.md"
    if not plan_file.exists():
        return 0, 0
    content = plan_file.read_text()
    return parse_task_counts(content)


def read_file_if_exists(path: Path) -> str | None:
    """Read a file and return its content, or None if it doesn't exist."""
    if path.exists():
        return path.read_text()
    return None
