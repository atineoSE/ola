"""CLI entry point for ola-dashboard."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from ola.dashboard.server import repo_dist_dir, serve


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the dashboard server until interrupted."""
    parser = argparse.ArgumentParser(
        prog="ola-dashboard",
        description=(
            "Browser dashboard for monitoring OLA agent progress. Serves the "
            "built SPA and a JSON API that reads the agent folder directly."
        ),
    )
    parser.add_argument(
        "-f",
        "--agent-folder",
        type=Path,
        default=Path("../agent"),
        help="Path to the agent folder (default: ../agent)",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8765,
        help="Port to listen on (default: 8765)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host/interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--dist",
        type=Path,
        default=None,
        help="Path to the built SPA (default: <repo>/dashboard/dist)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser window on startup",
    )
    args = parser.parse_args(argv)

    agent_folder = args.agent_folder.resolve()
    dist_dir = (args.dist or repo_dist_dir()).resolve()
    if not (dist_dir / "index.html").exists():
        parser.exit(
            1,
            f"No built dashboard at {dist_dir}.\n"
            "Build it first: `make dashboard` (or `npm --prefix dashboard run build`).\n",
        )

    httpd = serve(
        agent_folder, host=args.host, port=args.port, dist_dir=dist_dir, quiet=False
    )
    url = f"http://{args.host}:{args.port}/"
    print(f"ola-dashboard serving {agent_folder} at {url}", file=sys.stderr)
    if not args.no_browser:
        # Defer so the server is accepting connections before the tab opens.
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
