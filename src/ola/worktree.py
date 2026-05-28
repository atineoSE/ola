"""Per-task git worktree management for parallel agent runs.

Each task gets its own worktree branched off the agent-folder HEAD. The
agent runs there in isolation. When the agent finishes, the worktree's
commit is cherry-picked back onto the agent-folder branch under a lock,
excluding any paths that the scheduler propagates separately (notably
the folder's PLAN.md, so concurrent ticks don't conflict on shared lines).

merge_back leaves the cherry-picked changes staged but uncommitted so the
scheduler can fold additional edits (e.g. ``set_task_checked``) into the
same final commit.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class MergeBackConflict(Exception):
    """Raised when cherry-pick leaves unresolved conflicts outside exclude_paths."""

    def __init__(self, worktree: Path, sha: str, conflicted_paths: list[str]) -> None:
        self.worktree = worktree
        self.sha = sha
        self.conflicted_paths = conflicted_paths
        super().__init__(
            f"merge_back: unresolved conflicts in "
            f"{conflicted_paths} while cherry-picking {sha} from {worktree}"
        )


def _git(
    cwd: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True)
    if check and result.returncode != 0:
        logger.error(
            "git %s (in %s) failed: %s",
            " ".join(args),
            cwd,
            result.stderr.decode(errors="replace"),
        )
        result.check_returncode()
    return result


def create(folder: Path, task_id: str) -> Path:
    """Create a git worktree for *task_id* anchored at the folder's HEAD.

    The worktree lives at ``<folder>/.ola/worktrees/<task_id>`` and tracks
    a fresh branch ``ola/<folder.name>/<task_id>``. Returns the worktree path.
    """
    folder = Path(folder)
    worktree_path = folder / ".ola" / "worktrees" / task_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"ola/{folder.name}/{task_id}"
    _git(folder, "worktree", "add", "-b", branch, str(worktree_path), "HEAD")
    return worktree_path


def commit(worktree: Path, message: str) -> str:
    """Stage all changes in *worktree* and commit with *message*.

    Returns the resulting commit SHA. If the worktree has no uncommitted
    changes (e.g. the agent already committed), returns the current HEAD
    SHA without creating a new commit.
    """
    worktree = Path(worktree)
    _git(worktree, "add", "-A")
    staged = _git(worktree, "diff", "--cached", "--name-only").stdout.decode().strip()
    if staged:
        _git(worktree, "commit", "-m", message)
    return _git(worktree, "rev-parse", "HEAD").stdout.decode().strip()


def merge_back(
    worktree: Path,
    agent_root: Path,
    exclude_paths: list[str | Path] | None = None,
) -> str:
    """Cherry-pick *worktree*'s HEAD into *agent_root*, excluding *exclude_paths*.

    Runs ``git cherry-pick -n`` followed by ``git restore --staged --worktree
    --source=HEAD -- <exclude_paths>`` so the excluded paths are reverted to
    *agent_root*'s HEAD state (resolving any conflicts on them). Any remaining
    unmerged paths constitute a real conflict and raise :class:`MergeBackConflict`
    after rolling back the partial cherry-pick.

    Leaves non-excluded changes staged in *agent_root* without committing —
    the caller is responsible for the final ``git commit -C <sha>`` so it can
    bundle additional edits (e.g. PLAN.md tick via ``set_task_checked``) into
    the same commit. Returns the worktree's HEAD SHA for that commit.

    Excluded paths are interpreted relative to *agent_root* (git's cwd).

    Callers must serialise concurrent invocations against *agent_root* with
    their own lock — git's index is not safe under concurrent writes.
    """
    worktree = Path(worktree)
    agent_root = Path(agent_root)
    excluded = [str(p) for p in (exclude_paths or [])]

    sha = _git(worktree, "rev-parse", "HEAD").stdout.decode().strip()

    cp = _git(agent_root, "cherry-pick", "-n", sha, check=False)

    # Cherry-pick exits 0 on clean apply, 1 on conflict, anything else is a
    # fatal error (e.g. bad sha) — those are not recoverable by exclude_paths.
    if cp.returncode not in (0, 1):
        logger.error(
            "cherry-pick of %s failed: %s",
            sha,
            cp.stderr.decode(errors="replace"),
        )
        cp.check_returncode()

    if excluded:
        _git(
            agent_root,
            "restore",
            "--staged",
            "--worktree",
            "--source=HEAD",
            "--",
            *excluded,
            check=False,
        )

    unmerged = _git(agent_root, "ls-files", "--unmerged").stdout.decode().strip()
    if unmerged:
        conflicted = sorted({line.split("\t", 1)[1] for line in unmerged.splitlines()})
        # Roll back the partial cherry-pick so the caller's working tree
        # is left in a clean state.
        _git(agent_root, "reset", "--hard", "HEAD", check=False)
        raise MergeBackConflict(worktree=worktree, sha=sha, conflicted_paths=conflicted)

    return sha


def cleanup(worktree: Path, keep_on_failure: bool) -> None:
    """Remove *worktree* (registration + directory), unless preserving on failure.

    When *keep_on_failure* is True, the worktree is left in place and the
    function only logs. Use this on failure paths so the on-disk state stays
    available for post-mortem debugging.
    """
    worktree = Path(worktree)
    if keep_on_failure:
        logger.info("Preserving worktree at %s (keep_on_failure=True)", worktree)
        return
    if not worktree.exists():
        return
    common = (
        _git(worktree, "rev-parse", "--git-common-dir", check=False)
        .stdout.decode()
        .strip()
    )
    if common:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (worktree / common_path).resolve()
        main_tree = common_path.parent
        _git(
            main_tree,
            "worktree",
            "remove",
            "--force",
            str(worktree),
            check=False,
        )
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
