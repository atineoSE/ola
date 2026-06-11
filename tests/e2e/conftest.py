"""Pytest fixtures for the ola end-to-end suite.

Keeps the suite hermetic: git's global/system config is redirected to a
throwaway file so the pipeline's ``git config --global`` calls never touch the
developer's real ``~/.gitconfig``, and a temp ``HOME`` keeps any stray writes
contained.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_git_env(tmp_path, monkeypatch):
    """Redirect git global/system config and HOME to throwaway locations."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = E2E\n\temail = e2e@example.com\n[commit]\n\tgpgsign = false\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("HOME", str(tmp_path))
    yield
