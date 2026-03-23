import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.stats import IterationStats

logger = logging.getLogger(__name__)

_CREDENTIAL_FILES = (".credentials.json",)


class ClaudeCodeAgent(Agent):
    """Agent that delegates to the Claude Code CLI."""

    state_dir_name = ".claude"

    def run(
        self, prompt: str, workdir: str, state_dir: str | None = None
    ) -> AgentResponse:
        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            "-p",
            prompt,
        ]
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
            return self._parse_response(result)
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

    def _parse_response(self, result: subprocess.CompletedProcess) -> AgentResponse:
        """Parse JSON output from Claude Code CLI."""
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return AgentResponse(
                output=result.stdout + result.stderr,
                success=result.returncode == 0,
            )

        output = data.get("result", "")
        success = data.get("subtype") == "success"
        usage = data.get("usage", {})

        input_tokens = usage.get("input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

        stats = IterationStats(
            input_tokens=input_tokens + cache_creation + cache_read,
            output_tokens=usage.get("output_tokens", 0),
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            num_turns=data.get("num_turns", 0),
        )

        return AgentResponse(output=output, success=success, stats=stats)
