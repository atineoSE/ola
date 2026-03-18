import logging
import subprocess

from oloopa.agents.base import Agent, AgentResponse

logger = logging.getLogger(__name__)


class ClaudeCodeAgent(Agent):
    """Agent that delegates to the Claude Code CLI."""

    def run(self, prompt: str, workdir: str) -> AgentResponse:
        cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt]
        if self.model:
            cmd.extend(["--model", self.model])

        logger.debug("Running: %s", " ".join(cmd[:3]) + " ...")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=workdir,
                timeout=600,
            )
            output = result.stdout + result.stderr
            return AgentResponse(output=output, success=result.returncode == 0)
        except subprocess.TimeoutExpired:
            logger.error("Claude Code timed out after 600s")
            return AgentResponse(
                output="Claude Code timed out after 600s", success=False
            )
        except FileNotFoundError:
            logger.error("'claude' CLI not found")
            return AgentResponse(
                output="'claude' CLI not found. Install it first.",
                success=False,
            )
