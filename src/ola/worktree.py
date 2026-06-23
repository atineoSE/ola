"""Per-task git worktree management for parallel agent runs.

Each task gets its own worktree branched off the *project* repo's HEAD (the
process cwd, where the agent edits the project). The agent runs there in
isolation. When the agent finishes, the worktree's commit is 3-way merged
back onto the project repo's branch under a lock. The checkbox tick lives in
the separate agent folder and is committed there by the scheduler, so it is
never part of this merge.

merge_back leaves the merged changes staged but uncommitted so the scheduler
can commit them with the agent's original message (``git commit -C <sha>``).
The merge is computed in the object store (``git merge-tree``) so a collision
with an existing path in the project tree — even a byte-identical untracked
one a sibling task just landed — reconciles instead of aborting the apply.

Sandbox notes
-------------
The secondary worktree's ``.git`` is a *file* containing
``gitdir: <main-repo>/.git/worktrees/<name>``, not a directory. Anything
that hard-codes ``test -d .git`` (or a similar assumption) will fail
inside a worktree — use ``git rev-parse --git-dir`` instead. This also
means the worktree dir is only self-contained as long as the referenced
``.git/worktrees/<name>`` path is reachable through the same filesystem
view; inside ``ola-sandbox`` both live under the bind-mounted project
tree so the reference resolves. ``tests/test_sandbox_worktree.bats`` is
a one-shot smoke test that exercises the full create → commit →
cherry-pick → remove cycle inside the sandbox to guard against this.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class MergeBackConflict(Exception):
    """Raised when the 3-way merge leaves conflicts outside exclude_paths."""

    def __init__(self, worktree: Path, sha: str, conflicted_paths: list[str]) -> None:
        self.worktree = worktree
        self.sha = sha
        self.conflicted_paths = conflicted_paths
        super().__init__(
            f"merge_back: unresolved conflicts in "
            f"{conflicted_paths} while merging {sha} from {worktree}"
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


def create(project_path: Path, folder: Path, task_id: str) -> Path:
    """Create a git worktree for *task_id* anchored at *project_path*'s HEAD.

    The worktree lives at ``<project_path>/.ola/worktrees/<task_id>`` and tracks
    a fresh branch ``ola/<folder.name>/<task_id>`` (named after the agent-folder
    stage *folder* for traceability). Returns the worktree path.

    Idempotent: any stale worktree or branch left over from a prior attempt
    (e.g. a failed task being retried under ``--max-attempts``) is cleared
    first so the fresh ``worktree add`` always succeeds. On a first creation
    there is nothing to clear and the teardown commands are harmless no-ops.
    """
    project_path = Path(project_path)
    worktree_path = project_path / ".ola" / "worktrees" / task_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"ola/{folder.name}/{task_id}"
    # Clear leftovers from a prior attempt before recreating.
    _git(project_path, "worktree", "remove", "--force", str(worktree_path), check=False)
    _git(project_path, "worktree", "prune", check=False)
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    _git(project_path, "branch", "-D", branch, check=False)
    _git(project_path, "worktree", "add", "-b", branch, str(worktree_path), "HEAD")
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
    repo: Path,
    exclude_paths: list[str | Path] | None = None,
) -> str:
    """Reconcile *worktree*'s changes into *repo* via a 3-way merge.

    *repo* is the project repo the worktree was branched from. The merge is a
    real 3-way reconciliation computed in the object store with ``git merge-tree
    --write-tree``: the merge base is the common ancestor (``git merge-base``),
    so every commit the agent made on its branch is folded in and git
    auto-resolves non-overlapping edits exactly as an ordinary merge would.

    Working the result out at the tree level — rather than ``git cherry-pick
    -n`` against the live working tree — is what makes a slow/parallel run
    robust: an incoming add that collides with an *existing* path in the project
    tree (even an untracked, byte-identical one such as an empty ``__init__.py``
    a sibling task just landed) is reconciled instead of aborting the apply with
    exit 128. An identical add is, by construction, no diff against HEAD, so it
    simply drops out of the merge.

    The merged tree is then read into *repo*'s index and working tree
    (``read-tree --reset -u``), staged against HEAD and ready for the caller's
    ``git commit -C <sha>``. ``--reset -u`` discards a colliding untracked file
    without aborting while leaving git-ignored runtime state (the ``.ola/``
    worktrees) in place.

    *exclude_paths* are reverted to *repo*'s HEAD state after the merge (any
    incoming changes to them are dropped); a conflict confined entirely to
    excluded paths is therefore not a real conflict. Any conflict on a
    non-excluded path raises :class:`MergeBackConflict` after a defensive
    ``reset --hard`` leaves the project tree clean.

    Leaves the merged changes staged in *repo* without committing — the caller
    is responsible for the final ``git commit -C <sha>`` to preserve the agent's
    original commit message (and should skip the commit when nothing landed, the
    pure identical-add case). Returns the worktree's HEAD SHA for that commit.

    Excluded paths are interpreted relative to *repo* (git's cwd).

    Callers must serialise concurrent invocations against *repo* with their
    own lock — git's index is not safe under concurrent writes.
    """
    worktree = Path(worktree)
    repo = Path(repo)
    excluded = [str(p) for p in (exclude_paths or [])]

    sha = _git(worktree, "rev-parse", "HEAD").stdout.decode().strip()
    # The common ancestor is the project HEAD the worktree was branched from;
    # worktrees share the object store, so *repo* can resolve *sha*.
    base = _git(repo, "merge-base", "HEAD", sha).stdout.decode().strip()

    mt = _git(
        repo,
        "merge-tree",
        "--write-tree",
        "--name-only",
        f"--merge-base={base}",
        "HEAD",
        sha,
        check=False,
    )
    # merge-tree exits 0 on a clean (or fully auto-resolved) merge and 1 when
    # conflicts remain; anything else is a fatal error (bad sha, bad merge-base)
    # not recoverable here. The first stdout line is always the merged tree OID.
    if mt.returncode not in (0, 1):
        logger.error(
            "merge-tree of %s failed: %s",
            sha,
            mt.stderr.decode(errors="replace"),
        )
        mt.check_returncode()

    lines = mt.stdout.decode().splitlines()
    merged_tree = lines[0].strip() if lines else ""

    if mt.returncode == 1:
        # With --name-only the conflicted paths follow the tree OID, one per
        # line, until the first blank line (the rest is informational). Excluded
        # paths are reverted to HEAD anyway, so drop them before judging.
        conflicted: list[str] = []
        for line in lines[1:]:
            if not line.strip():
                break
            conflicted.append(line)
        real = sorted({p for p in conflicted if p not in excluded})
        if real:
            # Nothing has touched the project index/working tree yet, but reset
            # to HEAD defensively so the tree is unambiguously clean on raise.
            _git(repo, "reset", "--hard", "HEAD", check=False)
            raise MergeBackConflict(worktree=worktree, sha=sha, conflicted_paths=real)

    # Land the merged tree: index + working tree become *merged_tree*, staged
    # against HEAD. --reset tolerates (overwrites) colliding untracked files.
    _git(repo, "read-tree", "--reset", "-u", merged_tree)

    if excluded:
        _git(
            repo,
            "restore",
            "--staged",
            "--worktree",
            "--source=HEAD",
            "--",
            *excluded,
            check=False,
        )

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
