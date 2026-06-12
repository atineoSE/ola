"""Route smoke tests for the ola-dashboard server.

Spins up the real ``ThreadingHTTPServer`` on an ephemeral port and drives it
with ``urllib`` so the routing, JSON shape, static serving, and the
concurrency write are all exercised end to end.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from ola.dashboard.server import serve
from ola.scheduler import read_concurrency


def _write_parallel_folder(agent: Path, name: str) -> Path:
    folder = agent / name
    ola = folder / ".ola"
    ola.mkdir(parents=True)
    (ola / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "t-aaa",
                        "text": "First",
                        "line_no": 1,
                        "status": "complete",
                    }
                ]
            }
        )
    )
    (ola / "events.jsonl").write_text(
        json.dumps(
            {"task_id": "t-aaa", "status": "started", "ts": "2026-05-27T14:00:00.000Z"}
        )
        + "\n"
        + json.dumps(
            {
                "task_id": "t-aaa",
                "status": "complete",
                "ts": "2026-05-27T14:00:05.000Z",
                "agent_backend": "cc",
                "task_text": "First",
                "data": {},
            }
        )
        + "\n"
    )
    return folder


@contextmanager
def _running(agent: Path, dist: Path):
    httpd = serve(agent, host="127.0.0.1", port=0, dist_dir=dist)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
        return resp.status, resp.read()


def _headers(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
        resp.read()
        return resp.headers


def _missing_headers(url: str):
    try:
        urllib.request.urlopen(url, timeout=5)  # noqa: S310
    except urllib.error.HTTPError as exc:
        return exc.headers
    raise AssertionError(f"expected {url} to 404")


def _put_json(url: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310
        url, data=data, method="PUT", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        return resp.status, resp.read()


@pytest.fixture
def agent_and_dist(tmp_path: Path) -> tuple[Path, Path]:
    agent = tmp_path / "agent"
    agent.mkdir()
    _write_parallel_folder(agent, "09-par")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>ola-dashboard</title>")
    return agent, dist


def test_snapshot_route_returns_built_snapshot(agent_and_dist):
    agent, dist = agent_and_dist
    with _running(agent, dist) as base:
        status, body = _get(f"{base}/api/snapshot")
    assert status == 200
    snap = json.loads(body)
    assert set(snap) == {
        "first_started_ts",
        "counters",
        "tasks",
        "folders",
        "activity",
    }
    assert snap["tasks"]["t-aaa"]["status"] == "complete"
    assert "09-par" in snap["folders"]
    assert [a["task_id"] for a in snap["activity"]] == ["t-aaa"]


def test_static_index_is_served(agent_and_dist):
    agent, dist = agent_and_dist
    with _running(agent, dist) as base:
        status, body = _get(f"{base}/")
    assert status == 200
    assert b"ola-dashboard" in body


def test_cache_control_keeps_the_shell_fresh_but_assets_immutable(agent_and_dist):
    """The SPA shell must revalidate so a rebuild can't strand the browser on
    deleted bundle names; content-hashed assets cache hard, but a missing one
    is never cached immutable."""
    agent, dist = agent_and_dist
    (dist / "assets").mkdir()
    (dist / "assets" / "index-abc123.js").write_text("// bundle")
    with _running(agent, dist) as base:
        shell = _headers(f"{base}/")["Cache-Control"]
        asset = _headers(f"{base}/assets/index-abc123.js")["Cache-Control"]
        api = _headers(f"{base}/api/snapshot")["Cache-Control"]
        missing = _missing_headers(f"{base}/assets/index-deadbeef.js")["Cache-Control"]
    assert shell == "no-cache"
    assert asset == "public, max-age=31536000, immutable"
    assert api == "no-store"
    assert missing == "no-cache"  # a 404 must not be cached forever


def test_concurrency_get_null_then_put_then_get(agent_and_dist):
    agent, dist = agent_and_dist
    with _running(agent, dist) as base:
        status, body = _get(f"{base}/api/concurrency?folder=09-par")
        assert status == 200
        assert json.loads(body) == {"folder": "09-par", "concurrency": None}

        status, _ = _put_json(
            f"{base}/api/concurrency", {"folder": "09-par", "concurrency": 4}
        )
        assert status == 202

        status, body = _get(f"{base}/api/concurrency?folder=09-par")
        assert json.loads(body)["concurrency"] == 4

    # The write landed on disk where the scheduler reads it.
    assert read_concurrency(agent / "09-par") == 4


def test_unknown_folder_is_404(agent_and_dist):
    agent, dist = agent_and_dist
    with _running(agent, dist) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}/api/concurrency?folder=does-not-exist")
    assert exc.value.code == 404


def test_path_traversal_folder_is_rejected(agent_and_dist):
    agent, dist = agent_and_dist
    with _running(agent, dist) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}/api/concurrency?folder=..%2f..")
    assert exc.value.code == 404


def test_unknown_api_route_is_404(agent_and_dist):
    agent, dist = agent_and_dist
    with _running(agent, dist) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}/api/bogus")
    assert exc.value.code == 404


def test_put_negative_concurrency_is_400(agent_and_dist):
    agent, dist = agent_and_dist
    with _running(agent, dist) as base:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _put_json(
                f"{base}/api/concurrency", {"folder": "09-par", "concurrency": -1}
            )
    assert exc.value.code == 400


def test_auto_port_falls_forward_when_preferred_is_taken(agent_and_dist):
    agent, dist = agent_and_dist
    # Occupy a preferred port, then ask for the same one with auto_port on.
    first = serve(agent, host="127.0.0.1", port=0, dist_dir=dist)
    taken = first.server_address[1]
    try:
        second = serve(
            agent, host="127.0.0.1", port=taken, dist_dir=dist, auto_port=True
        )
        try:
            assert second.server_address[1] != taken
            assert taken < second.server_address[1] < taken + 64
        finally:
            second.server_close()
    finally:
        first.server_close()
