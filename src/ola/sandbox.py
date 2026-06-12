import os
from pathlib import Path

# Resolved env snapshot written into the sandbox by `ola-sandbox` (host-side),
# loaded in preference to the mounted agent/.env when running in a sandbox.
# It lives outside the mounted workspace so it never touches the user's repo.
SIDECAR_ENV = Path.home() / ".ola" / "agent.env"


def is_sandbox() -> bool:
    """Return True when running inside a Docker sandbox."""
    return os.getenv("SANDBOX") == "1"


def sanitize_proxy_env() -> None:
    """Drop bracketed IPv6 entries (e.g. ``[::1]``) from NO_PROXY/no_proxy.

    sbx v0.31.0 added a bracketed ``[::1]`` entry to ``NO_PROXY`` ("Add
    bracketed [::1] to NO_PROXY for IPv6 loopback"). httpx — used by litellm
    inside the OpenHands agent, and by other backends via ``trust_env`` — parses
    each no_proxy entry as a URL and rejects the bracketed form with
    ``InvalidURL: Invalid port: ':1]'``, which kills *every* LLM call before any
    network egress. Nothing in ola reads NO_PROXY, so the only leverage point is
    the environment itself: strip the bracketed entries here, before any agent
    backend (in-process or subprocess) builds an HTTP client. The bare ``::1``
    entry is left intact — httpx parses it fine.
    """
    for key in ("NO_PROXY", "no_proxy"):
        val = os.environ.get(key)
        if not val:
            continue
        kept = [e for e in val.split(",") if not e.strip().startswith("[")]
        cleaned = ",".join(kept)
        if cleaned != val:
            os.environ[key] = cleaned
