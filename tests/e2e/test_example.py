"""Hygiene guards for the shipped ``examples/dummy-project``.

The happy-path pipeline tests already run directly off the example's agent
folders (see ``test_pipeline.py``); these checks cover the parts the pipeline
doesn't exercise — chiefly that the example never ships secrets. The external
copy of this project once ended up with live API keys in ``.env``, so CI
enforces placeholders-only here.
"""

from __future__ import annotations

import re

from .harness import EXAMPLE_AGENT

SECRET_LIKE = re.compile(r"[A-Za-z0-9_\-]{16,}")


def test_example_ships_no_env_file():
    project = EXAMPLE_AGENT.parent
    assert not list(project.rglob(".env")), (
        "examples/dummy-project must never ship a filled-in .env — "
        "only .env.example with placeholders"
    )


def test_example_env_template_has_placeholders_only():
    for line in (EXAMPLE_AGENT / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        assert not SECRET_LIKE.search(value), (
            f"{key} in .env.example looks like a real credential: {value!r}"
        )
