"""Plan folder discovery and PLAN.md parsing."""

import re
from pathlib import Path


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
    return bool(re.search(r"- \[ \]", content))


def read_file_if_exists(path: Path) -> str | None:
    """Read a file and return its content, or None if it doesn't exist."""
    if path.exists():
        return path.read_text()
    return None
