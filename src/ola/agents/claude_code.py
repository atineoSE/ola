import logging
import os
import shutil
import subprocess
from pathlib import Path

from ola.agents.base import Agent, AgentResponse

logger = logging.getLogger(__name__)

_CREDENTIAL_FILES = (".credentials.json",)


class ClaudeCodeAgent(Agent):
    """Agent that delegates to the Claude Code CLI."""

    state_dir_name = ".claude"

    def run(
        self, prompt: str, workdir: str, state_dir: str | None = None
    ) -> AgentResponse:
        cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt]
        if self.model:
            cmd.extend(["--model", self.model])

        logger.debug("Running: %s", " ".join(cmd[:3]) + " ...")

        env = None
        if state_dir:
            sd = Path(state_dir)
            sd.mkdir(parents=True, exist_ok=True)
            home_claude = Path.home() / ".claude"
            for fname in _CREDENTIAL_FILES:
                src = home_claude / fname
                dst = sd / fname
                if src.exists() and not dst.exists():
                    shutil.copy2(src, dst)
                    logger.debug("Copied %s → %s", src, dst)
            env = {**os.environ, "CLAUDE_CONFIG_DIR": str(sd)}

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=workdir,
                timeout=600,
                env=env,
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
