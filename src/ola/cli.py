"""CLI entry point for ola."""

import argparse
import logging
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from ola.agents import create_agent
from ola.loop import run_outer_loop
from ola.sandbox import is_sandbox, sanitize_proxy_env
from ola.scheduler import (
    AUTH_ESCALATION_EXIT_CODE,
    AuthEscalation,
    FolderIncompleteError,
    RunInterrupted,
)

logger = logging.getLogger(__name__)


def _env_command(argv: list[str]) -> int:
    """`ola env` — host-side: validate the agent .env's host-sourced
    ${VAR} refs (fail-fast) and print the fully-resolved KEY="VALUE"
    snapshot to stdout. Consumed by `ola-sandbox` to set the network
    policy and to inject a resolved sidecar into the sandbox."""
    parser = argparse.ArgumentParser(
        prog="ola env",
        description="Validate & resolve the agent .env (host-side).",
    )
    parser.add_argument(
        "-f",
        "--agent-folder",
        type=str,
        default="../agent",
        help="Path to the agent folder (default: ../agent)",
    )
    args = parser.parse_args(argv)

    from ola.envresolve import MissingHostVars, format_sidecar, resolved_values

    env_file = Path(args.agent_folder).resolve() / ".env"
    if not env_file.is_file():
        # No .env: agents fall back to the ambient environment. Nothing to
        # resolve and nothing to fail on.
        return 0
    try:
        resolved = resolved_values(env_file)
    except MissingHostVars as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for line in format_sidecar(resolved):
        print(line)
    return 0


RUN_INIT_SCRIPT = "run-init.sh"


def _run_init(plan_path: Path, project_path: Path) -> None:
    """Run the agent folder's ``run-init.sh`` once, before any task is dispatched.

    The seam for project-level preconditions the harness itself must not know
    about: reclaiming a server a previous run leaked, clearing a cache, seeding
    a fixture. The agent folder declares them and ola only executes them — the
    same division as ``allowlist.txt`` (egress) and ``provision.sh`` (tooling),
    which keeps workload-specific knowledge out of the harness.

    Runs from the *project* repo, so a script can reach ``.ola/worktrees`` the
    way the rest of the run does. Output is not captured: it belongs in ola's
    log, interleaved with the run it precedes.

    Sandbox-only. Under ``--skip-sandbox`` this would execute project shell
    code against the developer's own machine, where the very thing it is most
    useful for — sweeping up stray processes — is the thing you least want
    aimed at a live workstation.

    A non-zero exit aborts the run: a precondition that failed is not a
    precondition, and discovering it task-by-task is strictly worse.
    """
    script = plan_path / RUN_INIT_SCRIPT
    if not script.is_file():
        return
    if not is_sandbox():
        logger.warning(
            "Skipping %s — it runs inside the sandbox only (see --skip-sandbox).",
            script,
        )
        return
    logger.info("Running %s", script)
    result = subprocess.run(["bash", str(script)], cwd=str(project_path))
    if result.returncode != 0:
        logger.error(
            "%s failed (exit %d); not starting the run.", script, result.returncode
        )
        sys.exit(1)


def main() -> None:
    # The process cwd is the *project* repo: per-task worktrees spawn from it
    # and the agent edits the project there. The agent folder (``-f``) only
    # carries the plan and the checkbox ticks. Capture cwd before any chdir.
    project_path = Path.cwd()

    if sys.argv[1:2] == ["env"]:
        sys.exit(_env_command(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        prog="ola",
        description="Outer Loop of Agents",
    )
    parser.add_argument(
        "-a",
        "--agent",
        choices=[
            "openhands",
            "oh",
            "claude-code",
            "cc",
            "claude-tui",
            "ct",
            "codex",
            "cx",
        ],
        default="cc",
        help="Agent to use (default: cc). 'ct' drives the interactive Claude "
        "Code TUI via a pty (no metrics — see ClaudeCodeTUIAgent).",
    )
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        default=None,
        help="Model name (default: agent-specific default)",
    )
    parser.add_argument(
        "-f",
        "--agent-folder",
        type=str,
        default="../agent",
        help="Path to the agent folder (default: ../agent)",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Max iterations per plan subfolder (default: no limit)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Run a failed/stagnant task up to K times total (default: 3)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Disable debug logging",
    )
    parser.add_argument(
        "--no-janitor",
        action="store_true",
        help="Disable the janitor: blocked tasks stay blocked instead of"
        " triggering an automatic unblock/escalate run",
    )
    parser.add_argument(
        "--metric-cmd",
        type=str,
        default=None,
        help="Shell command run in the project base branch after each "
        "merge-back (and once as a baseline); its JSON stdout ({name, value} "
        "object or array) is appended to .ola/metrics.jsonl. Expected to be "
        "idempotent. Disabled when unset.",
    )
    parser.add_argument(
        "--skip-sandbox",
        action="store_true",
        help="Allow running outside a Docker sandbox",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('ola')}",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.quiet else logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not is_sandbox() and not args.skip_sandbox:
        logger.error(
            "ola is not running inside a sandbox. "
            "Use --skip-sandbox to run outside a sandbox environment."
        )
        sys.exit(1)

    # Repair the sbx-injected NO_PROXY before any agent backend builds an HTTP
    # client (httpx chokes on the bracketed [::1] entry). See sanitize_proxy_env.
    sanitize_proxy_env()

    plan_path = Path(args.agent_folder).resolve()

    if not plan_path.is_dir():
        logger.error("%s is not a directory.", plan_path)
        sys.exit(1)

    _run_init(plan_path, project_path)

    agent = create_agent(args.agent, model=args.model)
    try:
        run_outer_loop(
            agent,
            plan_path,
            project_path,
            limit=args.limit,
            max_attempts=args.max_attempts,
            janitor_enabled=not args.no_janitor,
            metric_cmd=args.metric_cmd,
        )
    except FolderIncompleteError as exc:
        # A folder could not be driven to completion and its stuck tasks were
        # not relocated to leftovers/blockers. Stop rather than advance past
        # unfinished work — the only bail-out in the otherwise-autonomous run.
        logger.error("%s", exc)
        logger.error(
            "Folder %s is stuck. Resolve the failing task(s) (or move them to a "
            "blockers/leftovers folder) and re-run ola.",
            exc.folder_name,
        )
        sys.exit(1)
    except AuthEscalation as exc:
        # Auth is global — one task's authentication_error means every task
        # sharing this credential would fail the same way — so the scheduler
        # already aborted the whole run and dropped the host-visible marker
        # (<agent-folder>/monitor/auth-escalation.json). Exit with a distinct
        # code so this is unmistakable from a generic crash (1).
        logger.error(
            "%s Run `ola-sandbox <name>` to refresh credentials, then re-run "
            "ola to resume from PLAN.md.",
            exc,
        )
        sys.exit(AUTH_ESCALATION_EXIT_CODE)
    except RunInterrupted as exc:
        # Operator stopped the run (Ctrl-C / SIGTERM). The scheduler already
        # flushed a terminal snapshot for the in-flight tasks, so this is a
        # clean stop, not a crash. Exit 128+signum (130 for SIGINT) per the
        # shell convention. Re-running ola re-derives state from PLAN.md.
        logger.warning(
            "%s In-flight tasks were recorded as interrupted; re-run ola to "
            "resume from PLAN.md.",
            exc,
        )
        sys.exit(128 + exc.signum if exc.signum else 130)


if __name__ == "__main__":
    main()
