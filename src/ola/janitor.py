"""The ola janitor: aggressive, automatic unblocking of blocked tasks.

When a task reports itself BLOCKED (see :mod:`ola.blocked`), the scheduler
dispatches one janitor agent run. The janitor is a sibling agent — same
backend and model as the harness's configured agent — primed by
``JANITOR-PROMPT.md`` (which inlines the canonical ``CONTRACT.md``) to do
exactly one of two things:

1. **Unblock**: add prerequisite tasks to the current folder's PLAN.md and
   move the blocked task to a letter-suffixed ``…-leftovers`` sibling folder.
2. **Escalate**: create a ``…-blockers`` sibling folder with a BLOCKERS.md
   (no PLAN.md, so the harness skips it) for a human to resolve.

The janitor runs in the agent folder itself (not a worktree) because its
whole job is editing the live plan and creating sibling folders. It holds
the folder's plan lock for its entire run so concurrent task propagation
cannot race its PLAN.md edits or its commit.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from ola.agents.base import Agent
from ola.blocked import BlockedRecord
from ola.loop import _append_stats, _git_commit, per_task_state_dir

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "agents"
_FOLDER_NAME_RE = re.compile(r"^(\d+)([a-z]*)-(.+)$")

# Suffixes generated for janitor-created sibling folders, in allocation
# order: a…z, then za…zz, zza…zzz, … — the z-prefix extension keeps every
# later suffix sorting after earlier ones while staying before the next
# numeric index.
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def load_contract() -> str:
    """Return the canonical ola contract text shipped with the package."""
    return (_PROMPTS_DIR / "CONTRACT.md").read_text()


def _suffix_candidates() -> Iterator[str]:
    """Yield letter suffixes in sort-preserving allocation order."""
    prefix = ""
    while True:
        for letter in _LETTERS:
            yield prefix + letter
        prefix += "z"


def allocate_sibling_names(plan_root: Path, folder_name: str) -> tuple[str, str]:
    """Return the next two free sibling names for *folder_name*'s index.

    ``01-init`` → ``("01a-init-leftovers", "01b-init-blockers")`` when no
    suffixed siblings exist yet. A trailing ``-leftovers``/``-blockers`` is
    stripped from the base so a second-generation janitor running inside
    ``01a-init-leftovers`` allocates ``01b-init-leftovers``, not a stacked
    ``…-leftovers-leftovers``. Allocation re-scans the disk on every call,
    so names handed to a janitor that ends up only using one of them are
    simply reused later.
    """
    m = _FOLDER_NAME_RE.match(folder_name)
    if m is None:
        raise ValueError(f"Cannot parse plan folder name: {folder_name!r}")
    idx, _, base = m.groups()
    base = re.sub(r"-(leftovers|blockers)$", "", base)

    used: set[str] = set()
    for entry in Path(plan_root).iterdir():
        if not entry.is_dir():
            continue
        em = _FOLDER_NAME_RE.match(entry.name)
        if em is not None and em.group(1) == idx and em.group(2):
            used.add(em.group(2))

    free: list[str] = []
    for suffix in _suffix_candidates():
        if suffix not in used:
            free.append(suffix)
            if len(free) == 2:
                break
    return (
        f"{idx}{free[0]}-{base}-leftovers",
        f"{idx}{free[1]}-{base}-blockers",
    )


def build_janitor_prompt(
    folder: Path,
    plan_root: Path,
    record: BlockedRecord,
    task_text: str,
) -> str:
    """Substitute the janitor prompt template for one blocked task."""
    template = (_PROMPTS_DIR / "JANITOR-PROMPT.md").read_text()
    leftovers, blockers = allocate_sibling_names(plan_root, folder.name)
    escalate_hint = ""
    if folder.name.endswith("-leftovers"):
        escalate_hint = (
            "- Note: this task already went through one unblock cycle "
            "(this folder is itself a leftovers folder) — prefer ESCALATE "
            "unless the fix is now obvious.\n"
        )
    return (
        template.replace("{{contract}}", load_contract().rstrip())
        .replace("{{folder_name}}", folder.name)
        .replace("{{plan_path}}", str(folder / "PLAN.md"))
        .replace("{{task_text}}", task_text)
        .replace("{{task_id}}", record.task_id)
        .replace("{{reason}}", record.reason)
        .replace("{{escalate_hint}}", escalate_hint)
        .replace("{{leftovers_folder}}", leftovers)
        .replace("{{blockers_folder}}", blockers)
    )


def run_janitor(
    agent: Agent,
    folder: Path,
    agent_root: Path,
    record: BlockedRecord,
    task_text: str,
    attempt: int,
    plan_lock: threading.Lock,
    emitter: Any | None,
) -> bool:
    """Run one janitor pass for a blocked task. Returns True on success.

    Holds *plan_lock* for the whole agent run plus the follow-up commit:
    the janitor rewrites the live PLAN.md, so a concurrent ``_propagate``
    tick landing between its read and write would be lost, and its
    ``git add -A`` commit must not race a cherry-pick on the shared index.
    Workers that finish meanwhile queue on the lock; dispatch continues.
    """
    folder = Path(folder)
    agent_root = Path(agent_root)
    janitor_id = f"janitor-{record.task_id}"
    prompt = build_janitor_prompt(folder, agent_root, record, task_text)
    state_dir = per_task_state_dir(folder, agent, janitor_id)
    labels = {
        "folder": folder.name,
        "phase": "janitor",
        "task_id": record.task_id,
    }
    common: dict[str, Any] = {
        "agent_id": janitor_id,
        "attempt": attempt,
        "folder": folder.name,
        "task_id": record.task_id,
        "task_text": task_text,
        "agent_backend": agent.mnemonic,
    }

    logger.info(
        "Dispatching janitor for blocked task %s (%s)",
        record.task_id,
        record.reason,
    )
    if emitter is not None:
        emitter.started(**common, data={"role": "janitor"})

    last_working = [0.0]

    def on_progress(message: str, metrics: dict[str, Any] | None = None) -> None:
        # Coalesce to at most one working event per second, mirroring the
        # scheduler's _ProgressEmitter (not reused here: circular import).
        if emitter is None:
            return
        now = time.monotonic()
        if now - last_working[0] < 1.0:
            return
        last_working[0] = now
        data: dict[str, Any] = {"message": message}
        if metrics:
            data["metrics"] = metrics
        emitter.working(**common, data=data)

    with plan_lock:
        t0 = time.monotonic()
        response = agent.run(
            prompt,
            str(agent_root),
            state_dir=state_dir,
            labels=labels,
            on_progress=on_progress,
        )
        wall_ms = int((time.monotonic() - t0) * 1000)
        _git_commit(agent_root, f"ola: {folder.name} janitor {record.task_id}")

    _append_stats(folder, janitor_id, response.stats, wall_ms, agent)
    if emitter is not None:
        if response.success:
            emitter.complete(**common, data=None)
        else:
            emitter.failed(**common, data={"error": "janitor run failed"})
    if not response.success:
        logger.error("Janitor for task %s failed; task stays blocked.", record.task_id)
    return response.success
