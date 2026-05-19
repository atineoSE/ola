"""Resolve and validate the agent ``.env``, failing fast on missing host vars.

python-dotenv is the single interpolation engine. This module only decides
which ``${VAR}`` references must be supplied by the surrounding (host)
environment, enforces that they are present and non-empty, and produces the
fully-resolved values so the sandbox can be handed a concrete snapshot.

python-dotenv (1.x) interpolates ``${NAME}`` and ``${NAME:-default}`` only;
a bare ``$NAME`` is left literal, so it is *not* treated as a reference here.
A ``${NAME:-default}`` carries its own fallback and is therefore optional;
a plain ``${NAME}`` whose name is not assigned within the ``.env`` itself is
host-sourced and mandatory.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

# ${NAME} or ${NAME:-default}
_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")


class MissingHostVars(RuntimeError):
    """Raised when a mandatory host-sourced ``${VAR}`` is unset or empty."""

    def __init__(self, names: list[str], env_file: Path) -> None:
        self.names = names
        super().__init__(
            "Missing required host environment variable(s): "
            f"{', '.join(names)}\n"
            f"Referenced by {env_file} but unset or empty in the environment. "
            "Export them before running ola (the host environment must be "
            "sound before proceeding)."
        )


def _raw(env_file: Path) -> dict[str, str | None]:
    return dict(dotenv_values(env_file, interpolate=False))


def host_refs(env_file: Path) -> tuple[set[str], set[str]]:
    """Return ``(mandatory, optional)`` reference names that are *not*
    assigned within the ``.env`` itself (those are python-dotenv's job)."""
    raw = _raw(env_file)
    assigned = set(raw)
    mandatory: set[str] = set()
    optional: set[str] = set()
    for value in raw.values():
        if not value:
            continue
        for name, default in _REF.findall(value):
            if name in assigned:
                continue
            (optional if default else mandatory).add(name)
    optional -= mandatory  # a name used both ways is mandatory
    return mandatory, optional


def missing_host_vars(
    env_file: Path, source: Mapping[str, str] | None = None
) -> list[str]:
    """Sorted mandatory host refs that are unset or empty in ``source``
    (defaults to ``os.environ``)."""
    import os

    src: Mapping[str, str] = os.environ if source is None else source
    mandatory, _ = host_refs(env_file)
    return sorted(n for n in mandatory if not src.get(n))


def validate(env_file: Path, source: Mapping[str, str] | None = None) -> None:
    """Fail fast (raise :class:`MissingHostVars`) if any mandatory host
    reference is unset or empty."""
    missing = missing_host_vars(env_file, source)
    if missing:
        raise MissingHostVars(missing, env_file)


def resolved_values(env_file: Path) -> dict[str, str]:
    """Fully-resolved ``KEY=VALUE`` mapping, interpolated by python-dotenv
    against the current ``os.environ``. Validates first (fail-fast)."""
    validate(env_file)
    resolved = dotenv_values(env_file)
    return {k: ("" if v is None else v) for k, v in resolved.items()}


def format_sidecar(resolved: Mapping[str, str]) -> list[str]:
    """Serialize resolved values as double-quoted dotenv lines that
    python-dotenv parses back verbatim (no further interpolation needed —
    every value is already concrete)."""
    lines: list[str] = []
    for key, value in resolved.items():
        esc = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        lines.append(f'{key}="{esc}"')
    return lines
