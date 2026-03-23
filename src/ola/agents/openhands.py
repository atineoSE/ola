import logging
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

from ola.agents.base import Agent, AgentResponse
from ola.stats import IterationStats

logger = logging.getLogger(__name__)

_CONFIG_FILES = ("agent_settings.json", "cli_config.json")


class OpenHandsAgent(Agent):
    """Agent that delegates to OpenHands SDK."""

    state_dir_name = ".openhands"

    def run(
        self, prompt: str, workdir: str, state_dir: str | None = None
    ) -> AgentResponse:
        try:
            from openhands.sdk import LLM, Agent as OHAgent, Conversation, Tool
            from openhands.sdk.conversation.response_utils import (
                get_agent_final_response,
            )
            from openhands.sdk.logger.logger import setup_logging as oh_setup_logging
            from pydantic import SecretStr

            import openhands.tools  # noqa: F401 — registers TerminalTool, FileEditorTool
        except ImportError:
            logger.error("openhands-sdk or openhands-tools is not installed")
            return AgentResponse(
                output="openhands-sdk or openhands-tools is not installed.",
                success=False,
            )

        load_dotenv()

        base = Path(state_dir) if state_dir else Path(workdir)
        base.mkdir(parents=True, exist_ok=True)
        home_oh = Path.home() / ".openhands"
        for fname in _CONFIG_FILES:
            src = home_oh / fname
            dst = base / fname
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                logger.debug("Copied %s → %s", src, dst)
        oh_setup_logging(log_to_file=True, log_dir=str(base / "logs"))

        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            logger.error("LLM_API_KEY environment variable is not set")
            return AgentResponse(
                output="LLM_API_KEY environment variable is not set.",
                success=False,
            )

        model = self.model or os.getenv(
            "LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929"
        )
        base_url = os.getenv("LLM_BASE_URL")

        logger.debug("OpenHands agent using model=%s", model)

        llm = LLM(
            model=model,
            api_key=SecretStr(api_key),
            base_url=base_url,
        )

        agent = OHAgent(
            llm=llm,
            tools=[Tool(name="terminal"), Tool(name="file_editor")],
        )

        persistence_dir = str(base / "trajectories")
        conversation = Conversation(
            agent=agent, workspace=workdir, persistence_dir=persistence_dir
        )
        conversation.send_message(prompt)
        conversation.run()

        output = get_agent_final_response(conversation.state.events) or ""
        stats = self._extract_stats(conversation)
        return AgentResponse(output=output, success=True, stats=stats)

    def _extract_stats(self, conversation) -> IterationStats:
        """Extract token usage stats from conversation state."""
        try:
            usage_to_metrics = conversation.state.stats.usage_to_metrics
            total_input = 0
            total_output = 0
            total_cache_read = 0
            total_cache_write = 0

            for metrics in usage_to_metrics.values():
                acc = metrics.accumulated_token_usage
                total_input += acc.prompt_tokens
                total_output += acc.completion_tokens
                total_cache_read += acc.cache_read_tokens
                total_cache_write += acc.cache_write_tokens

            return IterationStats(
                # prompt_tokens already includes cache reads in OH
                input_tokens=total_input,
                output_tokens=total_output,
                cache_read_tokens=total_cache_read,
                cache_creation_tokens=total_cache_write,
            )
        except Exception as e:
            logger.warning("Could not extract OH stats: %s", e)
            return IterationStats()
