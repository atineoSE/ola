"""Tests for the ola-top CLI entry point."""

from __future__ import annotations

from unittest.mock import patch

from ola.monitor.cli import main


def test_default_args():
    """main() parses defaults and calls run_live with resolved path."""
    with patch("ola.monitor.cli.run_live") as mock_run:
        # Pass --help would exit; instead pass a custom folder
        main(["-f", "/tmp/fake-agent"])
        mock_run.assert_called_once()
        (args_path,) = mock_run.call_args.args  # positional
        assert args_path.name == "fake-agent"
        assert mock_run.call_args.kwargs["refresh_interval"] == 2.0


def test_custom_refresh():
    """Refresh interval is forwarded correctly."""
    with patch("ola.monitor.cli.run_live") as mock_run:
        main(["-f", "/tmp/fake-agent", "-r", "0.5"])
        assert mock_run.call_args.kwargs["refresh_interval"] == 0.5


def test_module_runnable():
    """python -m ola.monitor should be importable."""
    import ola.monitor.__main__  # noqa: F401
