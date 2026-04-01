"""Tests for the ClaudeCodeAgent."""

from unittest.mock import patch

from ola.agents.claude_code import AuthenticationError, ClaudeCodeAgent


class TestClaudeCodeAgent:
    def test_auth_error_returns_sbx_secret_message(self):
        """AuthenticationError produces an error message referencing `sbx secret`."""
        agent = ClaudeCodeAgent()
        with patch.object(agent, "_run_once", side_effect=AuthenticationError("bad")):
            resp = agent.run(prompt="hi", workdir="/tmp")
        assert not resp.success
        assert "sbx secret set -g anthropic" in resp.output

    def test_auth_error_does_not_mention_cc_credentials(self):
        """The old cc-credentials guidance should no longer appear."""
        agent = ClaudeCodeAgent()
        with patch.object(agent, "_run_once", side_effect=AuthenticationError("bad")):
            resp = agent.run(prompt="hi", workdir="/tmp")
        assert "cc-credentials" not in resp.output
