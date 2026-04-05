import logging
import os
import shutil
import time
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.stats import IterationStats

logger = logging.getLogger(__name__)

_lmnr_available = False
_lmnr_initialized = False


def _init_laminar():
    """Initialize Laminar lazily, only when OpenHands agent is actually used."""
    global _lmnr_available, _lmnr_initialized
    if _lmnr_initialized:
        return
    _lmnr_initialized = True
    try:
        from lmnr import Laminar

        _lmnr_base = os.getenv("LMNR_BASE_URL", "http://localhost")
        _lmnr_key = os.environ.pop("LMNR_PROJECT_API_KEY", None)
        if _lmnr_key:
            Laminar.initialize(
                project_api_key=_lmnr_key,
                base_url=_lmnr_base,
                http_port=int(os.getenv("LMNR_HTTP_PORT", "8000")),
                # gRPC (default) breaks in sbx/docker sandboxes because the
                # MITM proxy downgrades HTTP/2 → HTTP/1.x. Use OTLP/HTTP.
                force_http=True,
            )
            _lmnr_available = True
    except ImportError:
        pass


_CONFIG_FILES = ("agent_settings.json", "cli_config.json")
_POLICY_FILE = Path(__file__).resolve().parent / "NETWORK-POLICY.md"


class _TTFTTracker:
    """Track first/last chunk timestamps per response for TTFT calculation.

    Used as a streaming token callback during ``Conversation.run()`` to derive
    per-LLM-call TTFT from chunk timing combined with ``response_latencies``.
    """

    def __init__(self) -> None:
        self.first_chunk: dict[str, float] = {}  # response_id -> timestamp
        self.last_chunk: dict[str, float] = {}  # response_id -> timestamp

    def on_token(self, chunk: object) -> None:
        rid = getattr(chunk, "id", None)
        if rid is None:
            return
        now = time.monotonic()
        if rid not in self.first_chunk:
            self.first_chunk[rid] = now
        self.last_chunk[rid] = now

    def total_ttft_ms(self, response_latencies: list) -> int:
        """Derive total TTFT from chunk timing + per-call latencies.

        For each call: decode_time ≈ last_chunk - first_chunk,
        ttft ≈ total_latency - decode_time.
        """
        total = 0.0
        for rl in response_latencies:
            rid = rl.response_id
            if rid in self.first_chunk and rid in self.last_chunk:
                decode_secs = self.last_chunk[rid] - self.first_chunk[rid]
                ttft_secs = max(0.0, rl.latency - decode_secs)
                total += ttft_secs
        return int(total * 1000)


class OpenHandsAgent(Agent):
    """Agent that delegates to OpenHands SDK."""

    state_dir_name = ".openhands"
    mnemonic = "oh"
    full_name = "OpenHands"

    def version(self) -> str:
        try:
            from importlib.metadata import version

            return version("openhands-sdk")
        except Exception:
            return ""

    def run(
        self,
        prompt: str,
        workdir: str,
        state_dir: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentResponse:
        # Initialize Laminar BEFORE importing OpenHands SDK. The SDK
        # auto-instruments via lmnr at import time — if it sees
        # LMNR_PROJECT_API_KEY it sets up a gRPC exporter that breaks
        # behind the sbx proxy. Our init uses force_http=True and pops
        # the key so the SDK's auto-instrumentation is a no-op.
        _init_laminar()

        try:
            from openhands.sdk import (
                LLM,
                AgentContext,
                Agent as OHAgent,
                Conversation,
                Tool,
            )
            from openhands.sdk.context import Skill
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

        model_name = self.model or os.getenv(
            "LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929"
        )
        base_url = os.getenv("LLM_BASE_URL")

        logger.debug("OpenHands agent using model=%s", model_name)

        llm = LLM(
            model=model_name,
            api_key=SecretStr(api_key),
            base_url=base_url,
            stream=True,
        )

        network_policy = Skill(
            name="network-policy",
            content=_POLICY_FILE.read_text(),
            trigger=None,  # always active
        )
        agent = OHAgent(
            llm=llm,
            tools=[Tool(name="terminal"), Tool(name="file_editor")],
            agent_context=AgentContext(skills=[network_policy]),
        )

        tracker = _TTFTTracker()
        persistence_dir = str(base / "trajectories")
        conversation = Conversation(
            agent=agent,
            workspace=workdir,
            persistence_dir=persistence_dir,
            token_callbacks=[tracker.on_token],
        )

        labels = labels or {}
        folder = labels.get("folder", "")
        phase = labels.get("phase", "")

        if _lmnr_available:
            try:
                return self._run_with_tracing(
                    conversation,
                    prompt,
                    model_name,
                    folder,
                    phase,
                    get_agent_final_response,
                    tracker,
                )
            except Exception:
                logger.warning(
                    "Tracing failed, retrying without tracing", exc_info=True
                )

        conversation.send_message(prompt)
        conversation.run()
        output = get_agent_final_response(conversation.state.events) or ""
        stats = self._extract_stats(conversation, model_name, tracker)
        return AgentResponse(output=output, success=True, stats=stats)

    def _run_with_tracing(
        self,
        conversation,
        prompt: str,
        model_name: str,
        folder: str,
        phase: str,
        get_agent_final_response,
        tracker: _TTFTTracker,
    ) -> AgentResponse:
        """Run conversation inside a Laminar span with trace metadata."""
        from lmnr import Laminar

        span_name = f"ola-{self.mnemonic}"
        if folder:
            span_name += f"/{folder}"
        if phase:
            span_name += f"/{phase}"

        tags = [f"agent:{self.mnemonic}"]
        if phase:
            tags.append(phase)

        metadata = {"model": model_name}
        ver = self.version()
        if ver:
            metadata["agent_version"] = ver

        with Laminar.start_as_current_span(
            name=span_name,
            user_id=f"ola-{self.full_name.lower().replace(' ', '')}",
            session_id=folder,
            tags=tags,
            metadata=metadata,
        ):
            conversation.send_message(prompt)
            conversation.run()
            output = get_agent_final_response(conversation.state.events) or ""
            stats = self._extract_stats(conversation, model_name, tracker)
            Laminar.set_span_output(output[:500] if output else "")
            return AgentResponse(output=output, success=True, stats=stats)

    def _extract_stats(
        self, conversation, model: str = "", tracker: _TTFTTracker | None = None
    ) -> IterationStats:
        """Extract token usage and timing stats from conversation state."""
        try:
            usage_to_metrics = conversation.state.stats.usage_to_metrics
            total_input = 0
            total_output = 0
            total_cache_read = 0
            total_cache_write = 0
            total_llm_secs = 0.0
            num_turns = 0
            max_input_tokens = 0

            for metrics in usage_to_metrics.values():
                acc = metrics.accumulated_token_usage
                total_input += acc.prompt_tokens
                total_output += acc.completion_tokens
                total_cache_read += acc.cache_read_tokens
                total_cache_write += acc.cache_write_tokens
                # Sum LLM round-trip latencies (seconds) and count calls
                for rl in metrics.response_latencies:
                    total_llm_secs += rl.latency
                    num_turns += 1
                # Track max input context from per-call token usage
                for tu in metrics.token_usages:
                    turn_input = tu.prompt_tokens
                    if turn_input > max_input_tokens:
                        max_input_tokens = turn_input

            # Collect model names from usage keys; fall back to configured model.
            # The OpenHands SDK uses "default" as the key when only one LLM is
            # configured — replace it with the actual model name so ola-top
            # displays something meaningful.
            models = list(usage_to_metrics.keys()) if usage_to_metrics else []
            if model:
                models = [model if m == "default" else m for m in models]
                if not models:
                    models = [model]

            # Derive tool time: wall_ms is not known here yet (computed by
            # the outer loop), so we store the LLM time and let the caller
            # compute tool_ms = wall_ms - llm_ms after timing completes.
            # For now we store llm_ms and the loop will derive tool_ms.
            llm_ms = int(total_llm_secs * 1000)

            # Derive TTFT from streaming chunk timing
            ttft_ms = 0
            if tracker is not None:
                all_latencies = []
                for metrics in usage_to_metrics.values():
                    all_latencies.extend(metrics.response_latencies)
                ttft_ms = tracker.total_ttft_ms(all_latencies)

            return IterationStats(
                # prompt_tokens already includes cache reads in OH
                input_tokens=total_input,
                output_tokens=total_output,
                cache_read_tokens=total_cache_read,
                cache_creation_tokens=total_cache_write,
                num_turns=num_turns,
                models=models,
                llm_ms=llm_ms,
                max_input_tokens=max_input_tokens,
                ttft_ms=ttft_ms,
            )
        except Exception as e:
            logger.warning("Could not extract OH stats: %s", e)
            return IterationStats()
