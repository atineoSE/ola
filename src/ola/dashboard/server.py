"""Stateless HTTP server backing ola-dashboard.

Serves the built SPA (``dashboard/dist``) plus a tiny JSON API:

- ``GET  /api/snapshot``              → :func:`ola.monitor.data.build_snapshot`
- ``GET  /api/concurrency?folder=X``  → ``{"folder": X, "concurrency": int|null}``
- ``PUT  /api/concurrency``           → write ``<folder>/.ola/concurrency``

Every request re-reads the files; nothing is cached between requests. The
concurrency PUT is the dashboard's only write (the parallel-agents slider).
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ola.monitor.data import build_snapshot
from ola.scheduler import read_concurrency, write_concurrency

logger = logging.getLogger(__name__)

# Cap on a concurrency PUT body, a guard against a runaway request.
_MAX_BODY = 64 * 1024


def repo_dist_dir() -> Path:
    """Default location of the built SPA, relative to this source tree.

    ``src/ola/dashboard/server.py`` → repo-root ``dashboard/dist``. The
    dashboard is a dev/demo tool run from the checkout; ``--dist`` overrides.
    """
    return Path(__file__).resolve().parents[3] / "dashboard" / "dist"


class _Handler(SimpleHTTPRequestHandler):
    """Routes ``/api/*`` to the JSON API; everything else is a static file."""

    # Set per-server by :func:`make_handler`.
    agent_folder: Path
    quiet: bool = True

    def log_message(self, *args: object) -> None:  # noqa: D102
        if not self.quiet:
            super().log_message(*args)

    # --- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server casing)
        path = urlparse(self.path).path
        if path == "/api/snapshot":
            self._get_snapshot()
        elif path == "/api/concurrency":
            self._get_concurrency()
        elif path.startswith("/api/"):
            self._send_json({"detail": "not found"}, HTTPStatus.NOT_FOUND)
        else:
            super().do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/concurrency":
            self._put_concurrency()
        else:
            self._send_json({"detail": "not found"}, HTTPStatus.NOT_FOUND)

    # --- handlers --------------------------------------------------------

    def _get_snapshot(self) -> None:
        self._send_json(build_snapshot(self.agent_folder))

    def _get_concurrency(self) -> None:
        params = parse_qs(urlparse(self.path).query)
        name = (params.get("folder") or [""])[0]
        folder = self._resolve_folder(name)
        if folder is None:
            self._send_json({"detail": "unknown folder"}, HTTPStatus.NOT_FOUND)
            return
        # `null` distinguishes "no file yet" (scheduler default applies) from a
        # set value, matching the dashboard slider's expectation.
        exists = (folder / ".ola" / "concurrency").exists()
        value = read_concurrency(folder) if exists else None
        self._send_json({"folder": name, "concurrency": value})

    def _put_concurrency(self) -> None:
        body = self._read_json_body()
        if body is None:
            return  # error already sent
        name = body.get("folder")
        value = body.get("concurrency")
        folder = self._resolve_folder(name if isinstance(name, str) else "")
        if folder is None:
            self._send_json({"detail": "unknown folder"}, HTTPStatus.NOT_FOUND)
            return
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            self._send_json(
                {"detail": "concurrency must be a non-negative integer"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        write_concurrency(folder, value)
        self._send_json({"folder": name, "concurrency": value}, HTTPStatus.ACCEPTED)

    # --- helpers ---------------------------------------------------------

    def _resolve_folder(self, name: str | None) -> Path | None:
        """Resolve a folder name to a direct subdir of the agent folder.

        Rejects empty names, hidden folders, and anything that escapes the
        agent folder (path traversal), so a request can only address a real
        plan subfolder.
        """
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        base = self.agent_folder.resolve()
        target = (base / name).resolve()
        if target.parent != base or not target.is_dir():
            return None
        return target

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            self._send_json({"detail": "invalid body"}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            parsed = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._send_json({"detail": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(parsed, dict):
            self._send_json({"detail": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return None
        return parsed

    def _send_json(self, obj: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # API responses are always fresh; never let a browser cache them.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def make_handler(
    agent_folder: Path, dist_dir: Path, *, quiet: bool = True
) -> type[_Handler]:
    """Build a request-handler class bound to ``agent_folder`` and ``dist_dir``.

    ``SimpleHTTPRequestHandler`` takes its static root as a ``directory``
    constructor kwarg; a subclass injects it (and the agent folder) so the
    handler stays a plain ``BaseHTTPRequestHandler`` factory for the server.
    """

    class BoundHandler(_Handler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(dist_dir), **kwargs)  # type: ignore[arg-type]

    BoundHandler.agent_folder = agent_folder
    BoundHandler.quiet = quiet
    return BoundHandler


def serve(
    agent_folder: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    dist_dir: Path | None = None,
    quiet: bool = True,
) -> ThreadingHTTPServer:
    """Create (but do not start) a dashboard server bound to ``host:port``.

    Call ``serve_forever()`` on the returned server to run it. ``ThreadingHTTPServer``
    keeps snapshot polls and static-asset fetches from blocking each other.
    """
    dist = (dist_dir or repo_dist_dir()).resolve()
    handler = make_handler(agent_folder.resolve(), dist, quiet=quiet)
    return ThreadingHTTPServer((host, port), handler)
