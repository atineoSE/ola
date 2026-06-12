"""Stateless HTTP server backing ola-dashboard.

Serves the built SPA (``dashboard/dist``) plus a tiny JSON API:

- ``GET  /api/snapshot``              → :func:`ola.monitor.data.build_snapshot`
- ``GET  /api/concurrency?folder=X``  → ``{"folder": X, "concurrency": int|null}``
- ``PUT  /api/concurrency``           → write ``<folder>/.ola/concurrency``

Every request re-reads the files; nothing is cached between requests. The
concurrency PUT is the dashboard's only write (the parallel-agents slider).
"""

from __future__ import annotations

import errno
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
    # Set by _send_json so end_headers() leaves the API's own Cache-Control
    # (no-store) alone instead of layering a static-file policy on top.
    _api_response: bool = False
    # Last status handed to send_response(); gates the immutable asset header
    # so a 404 is never cached forever.
    _status: int = HTTPStatus.OK

    def log_message(self, *args: object) -> None:  # noqa: D102
        if not self.quiet:
            super().log_message(*args)

    # --- caching ---------------------------------------------------------

    def send_response(self, code: object, message: str | None = None) -> None:  # noqa: D102
        self._status = int(code)  # type: ignore[arg-type]
        super().send_response(code, message)  # type: ignore[arg-type]

    def end_headers(self) -> None:  # noqa: D102
        if not self._api_response:
            self._send_cache_control()
        super().end_headers()

    def _send_cache_control(self) -> None:
        """Cache policy for the static SPA.

        The shell (``index.html`` and friends) carries no content hash, so a
        rebuild reuses its URL; force revalidation or the browser keeps a stale
        shell pointing at deleted bundles — the 404 storm on old asset names.
        Files under ``/assets/`` are content-hashed by Vite, so a real hit is
        safe to cache hard; a 404 there must not be cached at all.
        """
        path = urlparse(self.path).path
        if path.startswith("/assets/") and self._status == HTTPStatus.OK:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")

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
        self._api_response = True  # keep end_headers() off this response
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


# How far to scan upward for a free port before giving up, when ``auto_port``
# is set. One dashboard per project means a handful of collisions at most.
_PORT_SCAN_RANGE = 64


def serve(
    agent_folder: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    dist_dir: Path | None = None,
    quiet: bool = True,
    auto_port: bool = False,
) -> ThreadingHTTPServer:
    """Create (but do not start) a dashboard server bound to ``host:port``.

    Call ``serve_forever()`` on the returned server to run it. ``ThreadingHTTPServer``
    keeps snapshot polls and static-asset fetches from blocking each other.

    With ``auto_port``, ``port`` is a *preferred* port: if it is already taken
    (another dashboard on another folder), scan upward for the first free one so
    running ``ola-dashboard`` in several checkouts just works. Read the chosen
    port back off ``server.server_address``. Without it, bind ``port`` exactly
    and let an in-use port raise — an explicit ``-p`` is a hard request.
    """
    dist = (dist_dir or repo_dist_dir()).resolve()
    handler = make_handler(agent_folder.resolve(), dist, quiet=quiet)
    if not auto_port:
        return ThreadingHTTPServer((host, port), handler)
    last_err: OSError | None = None
    for candidate in range(port, port + _PORT_SCAN_RANGE):
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EADDRNOTAVAIL):
                raise
            last_err = exc
    assert last_err is not None  # range is non-empty, so a failure was recorded
    raise last_err
