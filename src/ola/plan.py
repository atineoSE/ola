"""Plan folder discovery and PLAN.md parsing."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_CHECKBOX_RE = re.compile(r"^[ \t]*[-*+] \[( |x|X)\] (.*)$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")


@dataclass(frozen=True)
class Task:
    """A single checkbox line in PLAN.md."""

    task_id: str
    text: str
    line_no: int
    checked: bool


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


def _enumerate_tasks_from_text(text: str) -> list[Task]:
    """Walk *text* line-by-line, skipping fenced code blocks, yielding Tasks.

    Task ids are derived from a sha1 of the checkbox text; collisions get a
    `-2`, `-3`, ... suffix in enumeration order.
    """
    tasks: list[Task] = []
    seen_ids: dict[str, int] = {}
    in_fence = False
    for i, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        checked = m.group(1) != " "
        task_text = m.group(2).rstrip()
        base_id = "t-" + hashlib.sha1(task_text.encode("utf-8")).hexdigest()[:8]
        count = seen_ids.get(base_id, 0) + 1
        seen_ids[base_id] = count
        task_id = base_id if count == 1 else f"{base_id}-{count}"
        tasks.append(Task(task_id=task_id, text=task_text, line_no=i, checked=checked))
    return tasks


def enumerate_tasks(folder: Path) -> list[Task]:
    """Return the ordered list of checkbox tasks in *folder*'s PLAN.md.

    Skips fenced code blocks. Returns an empty list if PLAN.md is missing.
    """
    plan_file = folder / "PLAN.md"
    if not plan_file.exists():
        return []
    return _enumerate_tasks_from_text(plan_file.read_text())


_CHECKBOX_REWRITE_RE = re.compile(r"^([ \t]*[-*+] )\[( |x|X)\] (.*)$")


def _rewrite_checkbox(text: str, task_id: str, checked: bool) -> str:
    """Return *text* with the checkbox matching *task_id* set to *checked*.

    Raises KeyError if the id isn't found. Preserves indentation, bullet
    marker, line endings, and the absence of a trailing newline.
    """
    new_marker = "x" if checked else " "
    lines = text.splitlines(keepends=True)
    seen_ids: dict[str, int] = {}
    in_fence = False
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n").rstrip("\r")
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _CHECKBOX_REWRITE_RE.match(stripped)
        if m is None:
            continue
        task_text = m.group(3).rstrip()
        base_id = "t-" + hashlib.sha1(task_text.encode("utf-8")).hexdigest()[:8]
        count = seen_ids.get(base_id, 0) + 1
        seen_ids[base_id] = count
        this_id = base_id if count == 1 else f"{base_id}-{count}"
        if this_id != task_id:
            continue
        rewritten = f"{m.group(1)}[{new_marker}] {m.group(3)}"
        lines[idx] = rewritten + line[len(stripped) :]
        return "".join(lines)
    raise KeyError(f"task_id not found in PLAN.md: {task_id}")


def set_task_checked(folder: Path, task_id: str, checked: bool) -> None:
    """Tick or untick the checkbox for *task_id* in *folder*'s PLAN.md.

    Reads PLAN.md, rewrites the single matching line, and writes back
    atomically (tmp file + rename). Raises KeyError if the id isn't present.
    """
    plan_file = folder / "PLAN.md"
    text = plan_file.read_text()
    new_text = _rewrite_checkbox(text, task_id, checked)
    tmp = plan_file.with_name(plan_file.name + ".tmp")
    tmp.write_text(new_text)
    tmp.replace(plan_file)
