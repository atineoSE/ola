"""Tests for the ola CLI sandbox gate."""

import os
from unittest.mock import patch

import pytest

from ola.cli import main


class TestSandboxGate:
    """Verify ola refuses to run outside a sandbox unless --skip-sandbox is passed."""

    def test_exits_outside_sandbox_without_flag(self):
        """Outside sandbox and no --skip-sandbox → exit(1)."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sys.argv", ["ola", "-f", "/tmp/fake"]),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

    def test_runs_inside_sandbox(self, tmp_path):
        """Inside sandbox (SANDBOX=1) → proceeds normally."""
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-f", str(agent_dir)]),
            patch("ola.cli.create_agent") as mock_create,
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            mock_create.assert_called_once()
            mock_loop.assert_called_once()

    def test_runs_outside_sandbox_with_skip_flag(self, tmp_path):
        """Outside sandbox but --skip-sandbox passed → proceeds normally."""
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sys.argv", ["ola", "-f", str(agent_dir), "--skip-sandbox"]),
            patch("ola.cli.create_agent") as mock_create,
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            mock_create.assert_called_once()
            mock_loop.assert_called_once()

    def test_inside_sandbox_with_skip_flag(self, tmp_path):
        """Inside sandbox + --skip-sandbox → proceeds (flag is harmless)."""
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-f", str(agent_dir), "--skip-sandbox"]),
            patch("ola.cli.create_agent") as mock_create,
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            mock_create.assert_called_once()
            mock_loop.assert_called_once()


class TestMaxAttempts:
    """Verify --max-attempts routes into run_outer_loop (default 0)."""

    def test_default_is_zero(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-f", str(agent_dir)]),
            patch("ola.cli.create_agent"),
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            assert mock_loop.call_args.kwargs["max_attempts"] == 0

    def test_flag_reaches_run_outer_loop(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-f", str(agent_dir), "--max-attempts", "3"]),
            patch("ola.cli.create_agent"),
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            assert mock_loop.call_args.kwargs["max_attempts"] == 3


class TestAgentSelection:
    """Verify -a flag routes the requested agent name into create_agent."""

    @pytest.mark.parametrize("flag", ["codex", "cx"])
    def test_codex_flag_reaches_create_agent(self, tmp_path, flag):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-a", flag, "-f", str(agent_dir)]),
            patch("ola.cli.create_agent") as mock_create,
            patch("ola.cli.run_outer_loop"),
        ):
            main()
            mock_create.assert_called_once_with(flag, model=None)
