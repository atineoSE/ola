import os


def is_sandbox() -> bool:
    """Return True when running inside a Docker sandbox."""
    return os.getenv("SANDBOX") == "1"
