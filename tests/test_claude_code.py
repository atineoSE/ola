"""Tests for the ClaudeCodeAgent."""

from unittest.mock import patch

from ola.agents.claude_code import AuthenticationError, ClaudeCodeAgent


class TestClaudeCodeAgent:
    def test_auth_error_returns_credential_refresh_message(self):
        """AuthenticationError produces an error message referencing credential refresh."""
        agent = ClaudeCodeAgent()
        with patch.object(agent, "_run_once", side_effect=AuthenticationError("bad")):
            resp = agent.run(prompt="hi", workdir="/tmp")
        assert not resp.success
        assert "ola-sandbox" in resp.output
        assert ".credentials.json" in resp.output

    def test_auth_error_does_not_mention_old_approaches(self):
        """Neither cc-credentials nor sbx secret should appear."""
        agent = ClaudeCodeAgent()
        with patch.object(agent, "_run_once", side_effect=AuthenticationError("bad")):
            resp = agent.run(prompt="hi", workdir="/tmp")
        assert "cc-credentials" not in resp.output
        assert "sbx secret" not in resp.output
