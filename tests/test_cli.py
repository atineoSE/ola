"""Tests for the ola CLI sandbox gate."""

import os
from unittest.mock import patch

import pytest

from ola.cli import main
from ola.scheduler import AUTH_ESCALATION_EXIT_CODE, AuthEscalation, FolderIncompleteError


class TestBailOut:
    """A stuck folder (FolderIncompleteError) stops ola with a non-zero exit."""

    def test_folder_incomplete_exits_nonzero(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-f", str(agent_dir)]),
            patch("ola.cli.create_agent"),
            patch(
                "ola.cli.run_outer_loop",
                side_effect=FolderIncompleteError("02-utils", 1),
            ),
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == 1


class TestAuthEscalation:
    """AuthEscalation (whole-run auth abort) stops ola with a distinct exit code."""

    def test_auth_escalation_exits_with_distinct_code(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-f", str(agent_dir)]),
            patch("ola.cli.create_agent"),
            patch(
                "ola.cli.run_outer_loop",
                side_effect=AuthEscalation("02-utils", "bad credential"),
            ),
            pytest.raises(SystemExit) as excinfo,
        ):
            main()
        assert excinfo.value.code == AUTH_ESCALATION_EXIT_CODE
        assert excinfo.value.code != 1


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
    """Verify --max-attempts routes into run_outer_loop (default 3)."""

    def test_default_is_three(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-f", str(agent_dir)]),
            patch("ola.cli.create_agent"),
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            assert mock_loop.call_args.kwargs["max_attempts"] == 3

    def test_flag_reaches_run_outer_loop(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}),
            patch("sys.argv", ["ola", "-f", str(agent_dir), "--max-attempts", "5"]),
            patch("ola.cli.create_agent"),
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            assert mock_loop.call_args.kwargs["max_attempts"] == 5


class TestMetricProbe:
    """Verify --metric-cmd threads into run_outer_loop."""

    def test_defaults(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}, clear=True),
            patch("sys.argv", ["ola", "-f", str(agent_dir)]),
            patch("ola.cli.create_agent"),
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            assert mock_loop.call_args.kwargs["metric_cmd"] is None

    def test_flag_reaches_run_outer_loop(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(os.environ, {"SANDBOX": "1"}, clear=True),
            patch(
                "sys.argv",
                ["ola", "-f", str(agent_dir), "--metric-cmd", "echo hi"],
            ),
            patch("ola.cli.create_agent"),
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            assert mock_loop.call_args.kwargs["metric_cmd"] == "echo hi"

    def test_env_fallback_honored(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(
                os.environ,
                {"SANDBOX": "1", "OLA_METRIC_CMD": "probe.sh"},
                clear=True,
            ),
            patch("sys.argv", ["ola", "-f", str(agent_dir)]),
            patch("ola.cli.create_agent"),
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            assert mock_loop.call_args.kwargs["metric_cmd"] == "probe.sh"

    def test_flag_overrides_env(self, tmp_path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        with (
            patch.dict(
                os.environ,
                {"SANDBOX": "1", "OLA_METRIC_CMD": "from-env"},
                clear=True,
            ),
            patch(
                "sys.argv",
                ["ola", "-f", str(agent_dir), "--metric-cmd", "from-flag"],
            ),
            patch("ola.cli.create_agent"),
            patch("ola.cli.run_outer_loop") as mock_loop,
        ):
            main()
            assert mock_loop.call_args.kwargs["metric_cmd"] == "from-flag"


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
