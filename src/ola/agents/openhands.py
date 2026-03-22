import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from ola.agents.base import Agent, AgentResponse

logger = logging.getLogger(__name__)


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
            from openhands.sdk.logger import get_logger as oh_get_logger
            from openhands.tools.terminal import TerminalTool
            from openhands.tools.file_editor import FileEditorTool
            from pydantic import SecretStr
        except ImportError:
            logger.error("openhands-sdk is not installed")
            return AgentResponse(
                output="openhands-sdk is not installed. Install with: pip install openhands-sdk",
                success=False,
            )

        load_dotenv()

        base = Path(state_dir) if state_dir else Path(workdir)
        base.mkdir(parents=True, exist_ok=True)
        log_dir = str(base / "logs")
        oh_get_logger(__name__, log_dir=log_dir)

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
            tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)],
        )

        persistence_dir = str(base / "trajectories")
        conversation = Conversation(
            agent=agent, workspace=workdir, persistence_dir=persistence_dir
        )
        conversation.send_message(prompt)
        conversation.run()

        output = get_agent_final_response(conversation.state.events) or ""
        return AgentResponse(output=output, success=True)
