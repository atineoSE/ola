import os
from pathlib import Path

# Resolved env snapshot written into the sandbox by `ola-sandbox` (host-side),
# loaded in preference to the mounted agent/.env when running in a sandbox.
# It lives outside the mounted workspace so it never touches the user's repo.
SIDECAR_ENV = Path.home() / ".ola" / "agent.env"


def is_sandbox() -> bool:
    """Return True when running inside a Docker sandbox."""
    return os.getenv("SANDBOX") == "1"
