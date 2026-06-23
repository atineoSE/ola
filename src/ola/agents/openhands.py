import json
import logging
import os
import subprocess
import time
from pathlib import Path

from ola.agents.base import Agent, AgentResponse, ProgressCallback
from ola.stats import IterationStats

logger = logging.getLogger(__name__)

# The OpenHands CLI's --json mode (openhands_cli.utils.json_callback) does NOT
# emit clean JSONL. It prints this marker line, then a *pretty-printed,
# multi-line* JSON dump of one SDK event, repeated per event. The parser splits
# on the marker and accumulates the multi-line block in between.
_JSON_EVENT_MARKER = "--JSON Event--"

# LLM_* env knobs → agent_settings.json llm fields. Mirrors the surface the old
# SDK backend exposed; only set values are written, so SDK defaults fill the
# rest. (model/api_key/base_url are handled separately as required/identity
# fields and also re-applied via --override-with-envs.)
_ENV_LLM_OPTS: list[tuple[str, str, type]] = [
    ("timeout", "LLM_TIMEOUT", int),
    ("temperature", "LLM_TEMPERATURE", float),
    ("top_p", "LLM_TOP_P", float),
    ("max_input_tokens", "LLM_MAX_INPUT_TOKENS", int),
    ("max_output_tokens", "LLM_MAX_OUTPUT_TOKENS", int),
    ("reasoning_effort", "LLM_REASONING_EFFORT", str),
    ("num_retries", "LLM_NUM_RETRIES", int),
    ("extended_thinking_budget", "LLM_EXTENDED_THINKING_BUDGET", int),
    ("prompt_cache_retention", "LLM_PROMPT_CACHE_RETENTION", str),
]


def _resolve_localhost(url: str) -> str:
    """If *url* points to localhost and we're inside a sandbox, swap to
    ``host.docker.internal`` so the request can reach the host machine.

    Kept here (rather than moved) because ``ola.agents.codex`` imports it.
    """
    from urllib.parse import urlparse

    from ola.sandbox import is_sandbox

    if not is_sandbox():
        return url
    parsed = urlparse(url)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        return url
    return url.replace(parsed.hostname, "host.docker.internal", 1)


def _build_llm_config(model: str, api_key: str, base_url: str | None, usage_id: str) -> dict:
    """Build the ``llm`` block for agent_settings.json from LLM_* env vars.

    The on-disk ``agent_settings.json`` is a serialized OpenHands SDK ``Agent``
    (``Agent.model_validate_json``); its ``llm`` field is a full ``LLM`` model.
    We populate only the fields ola configures and let the SDK default the rest.
    The api_key is written in plaintext — the per-task state dir is private, and
    this mirrors what ``_ola_inject_oh_settings`` in ola.sh already does.
    """
    llm: dict = {
        "model": model,
        "api_key": api_key,
        "usage_id": usage_id,
        "stream": False,
        "drop_params": True,
    }
    if base_url:
        llm["base_url"] = base_url
    for key, envvar, typ in _ENV_LLM_OPTS:
        val = os.getenv(envvar)
        if val:
            llm[key] = typ(val)
    enc = os.getenv("LLM_ENABLE_ENCRYPTED_REASONING")
    if enc is not None:
        llm["enable_encrypted_reasoning"] = enc.lower() == "true"
    return llm


def _build_agent_settings(model: str, api_key: str, base_url: str | None) -> dict:
    """Render the agent_settings.json the headless CLI loads from the
    persistence dir.

    Minimal but complete: ``kind`` + ``llm`` + an ``LLMSummarizingCondenser``
    (so long-horizon runs get context summarization — the CLI only wires a
    condenser if one is already present in the persisted agent). Tools and
    agent-context are injected by the CLI's runtime config, so they are omitted
    here. Validated against the installed ``Agent`` schema.
    """
    return {
        "kind": "Agent",
        "llm": _build_llm_config(model, api_key, base_url, "agent"),
        "condenser": {
            "kind": "LLMSummarizingCondenser",
            "llm": _build_llm_config(model, api_key, base_url, "condenser"),
        },
    }


def _event_text(event: dict) -> str | None:
    """Derive a short human-readable status line from a parsed SDK event dict.

    Returns agent message text for ``MessageEvent`` (source agent), or
    ``[tool] summary`` for ``ActionEvent``; ``None`` for anything else.
    """
    kind = event.get("kind")
    if kind == "MessageEvent" and event.get("source") == "agent":
        parts = [
            c.get("text", "")
            for c in (event.get("llm_message") or {}).get("content") or []
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        joined = " ".join(p for p in parts if p).strip()
        return joined or None
    if kind == "ActionEvent":
        tool = event.get("tool_name") or "?"
        summary = (event.get("summary") or "").strip()
        return f"[{tool}] {summary}".strip()
    return None


def _agent_message_text(event: dict) -> str | None:
    """Full agent message text from a ``MessageEvent`` (source agent), else None.

    Used to track the conversation's final response; identical extraction to
    :func:`_event_text`'s message branch but without the action fallback.
    """
    if event.get("kind") == "MessageEvent" and event.get("source") == "agent":
        parts = [
            c.get("text", "")
            for c in (event.get("llm_message") or {}).get("content") or []
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return " ".join(p for p in parts if p).strip() or None
    return None


class OpenHandsAgent(Agent):
    """Agent that delegates to the OpenHands CLI in a subprocess.

    Unlike the former in-process SDK backend, this spawns ``openhands
    --headless --json`` per task. Subprocess isolation sidesteps the SDK's
    class-level ``_litellm_modify_params_lock`` (which serialized every LLM
    call to one in-flight request per process), so tasks run truly in parallel
    — the same model the ``cc``/``cx`` backends use.

    Headless ``--json`` emits high-level SDK events (not token-level chunks),
    so streaming timings (TTFT, decode-isolated tok/sec) are unavailable; token
    economics are recovered post-hoc from the persisted ``base_state.json``.
    """

    state_dir_name = ".openhands"
    mnemonic = "oh"
    full_name = "OpenHands"

    def version(self) -> str:
        try:
            result = subprocess.run(
                ["openhands", "--version"],
                capture_output=True,
                text=True,
                env={**os.environ, "OPENHANDS_SUPPRESS_BANNER": "1"},
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except FileNotFoundError:
            return ""

    def run(
        self,
        prompt: str,
        workdir: str,
        state_dir: str | None = None,
        labels: dict[str, str] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AgentResponse:
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            logger.error("LLM_API_KEY environment variable is not set")
            return AgentResponse(
                output="LLM_API_KEY environment variable is not set.",
                success=False,
            )

        if not state_dir:
            return AgentResponse(
                output="state_dir is required for OpenHandsAgent.",
                success=False,
            )

        model_name = self.model or os.getenv("LLM_MODEL")
        if not model_name:
            logger.error("no model configured (set LLM_MODEL or pass --model)")
            return AgentResponse(
                output="No model configured. Set LLM_MODEL or pass --model.",
                success=False,
            )

        base_url = os.getenv("LLM_BASE_URL") or None
        if base_url:
            base_url = _resolve_localhost(base_url)

        sd = Path(state_dir)
        sd.mkdir(parents=True, exist_ok=True)

        # Per-task agent_settings.json (full LLM config) at the persistence-dir
        # root the CLI reads (AGENT_SETTINGS_PATH is relative to it).
        settings = _build_agent_settings(model_name, api_key, base_url)
        (sd / "agent_settings.json").write_text(json.dumps(settings, indent=2))

        # Pass the prompt verbatim via -f (dodges argv length limits). Like the
        # cc/cx/ct backends, no network policy is injected — the sandbox
        # enforces the real boundary at the proxy.
        task_file = sd / "task.md"
        task_file.write_text(prompt)

        cmd = [
            "openhands",
            "--headless",
            "--json",
            "--override-with-envs",
            "-f",
            str(task_file),
        ]

        env = {
            **os.environ,
            "OPENHANDS_PERSISTENCE_DIR": str(sd),
            "OPENHANDS_WORK_DIR": workdir,
            "OPENHANDS_SUPPRESS_BANNER": "1",
            # --override-with-envs reads these three; ensure the live identity
            # always wins (sandbox substrate IP/key rotates between runs) and
            # that LLM_MODEL is present even when the model came from --model.
            "LLM_MODEL": model_name,
            "LLM_API_KEY": api_key,
        }
        if base_url:
            env["LLM_BASE_URL"] = base_url
        # Self-signed self-hosted endpoints: litellm reads $SSL_VERIFY. Set
        # per-subprocess (no shared-process race, unlike the old warm_up path).
        if os.getenv("LLM_SKIP_TLS_VERIFY", "").lower() == "true":
            env["SSL_VERIFY"] = "False"

        logger.debug("Running: %s", " ".join(cmd))
        logger.debug("OPENHANDS_PERSISTENCE_DIR=%s model=%s", sd, model_name)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=workdir,
                env=env,
            )
        except FileNotFoundError:
            logger.error("'openhands' CLI not found")
            return AgentResponse(
                output=(
                    "'openhands' CLI not found. "
                    "Install with `uv tool install openhands`."
                ),
                success=False,
            )

        return self._stream(proc, sd, model_name, on_progress=on_progress)

    def _stream(
        self,
        proc: subprocess.Popen,
        state_dir: Path,
        model_name: str,
        on_progress: ProgressCallback | None = None,
    ) -> AgentResponse:
        from ola.agents.codex import _StatusDisplay

        status = _StatusDisplay()
        last_progress_ts: float = 0.0  # monotonic; throttle on_progress to 1/s

        def _emit(text: str) -> None:
            nonlocal last_progress_ts
            status.update(text)
            if on_progress is None:
                return
            now = time.monotonic()
            if now - last_progress_ts < 1.0:
                return
            last_progress_ts = now
            try:
                on_progress(text)
            except Exception:
                logger.exception("on_progress callback raised; continuing")

        last_agent_message: str | None = None
        # Only a ConversationErrorEvent is fatal: the run loop moved to an ERROR
        # state (e.g. the LLM endpoint is unreachable, auth failed, rate limited).
        # An AgentErrorEvent is a recoverable tool observation, so it is ignored.
        fatal_error: str | None = None
        error_code: str | None = None

        for event in self._iter_events(proc.stdout):
            if event.get("kind") == "ConversationErrorEvent":
                error_code = event.get("code") or error_code
                # detail/code always populated on this event, so fatal_error is
                # never left falsy when one is seen (the bug that masked a dead
                # endpoint as success).
                fatal_error = (
                    event.get("detail")
                    or event.get("code")
                    or "conversation error"
                )
            msg = _agent_message_text(event)
            if msg:
                last_agent_message = msg
            line = _event_text(event)
            if line:
                _emit(line)

        status.clear()
        proc.wait()

        stats = self._extract_stats(state_dir, model_name)
        success = proc.returncode == 0 and fatal_error is None
        if not success:
            stderr = proc.stderr.read() if proc.stderr else ""
            error_message = fatal_error or (stderr[:500] if stderr else None)
            stats.error_type = error_code or (
                "conversation_error" if fatal_error else "nonzero_exit"
            )
            stats.error_message = error_message
            if error_message:
                low = error_message.lower()
                if "rate" in low and "limit" in low or "429" in low:
                    stats.error_type = "rate_limit"
            return AgentResponse(
                output=last_agent_message or fatal_error or stderr,
                success=False,
                stats=stats,
            )

        return AgentResponse(
            output=last_agent_message or "",
            success=True,
            stats=stats,
        )

    @staticmethod
    def _iter_events(stdout):
        """Yield parsed event dicts from the CLI's ``--JSON Event--`` stream.

        Events are pretty-printed multi-line JSON blocks separated by marker
        lines. We accumulate the lines of one block and parse it when the next
        marker (or EOF) arrives.

        ``raw_decode`` (not ``json.loads``) is essential: the CLI interleaves
        Rich console output (the "CONVERSATION SUMMARY" box, "Goodbye",
        "Conversation ID:") *after* the final event's JSON, in the same block.
        ``raw_decode`` parses the leading JSON object and ignores that trailing
        text; ``json.loads`` would choke on it and silently drop the last
        event (which is often the ConversationErrorEvent or final message).
        """
        decoder = json.JSONDecoder()
        buf: list[str] = []

        def flush(buf: list[str]):
            text = "".join(buf).strip()
            if not text or not text.startswith("{"):
                return None
            try:
                obj, _ = decoder.raw_decode(text)
                return obj
            except json.JSONDecodeError:
                return None

        for line in stdout:
            if line.strip() == _JSON_EVENT_MARKER:
                event = flush(buf)
                buf = []
                if event is not None:
                    yield event
            else:
                buf.append(line)
        event = flush(buf)
        if event is not None:
            yield event

    def _extract_stats(self, state_dir: Path, model_name: str) -> IterationStats:
        """Recover token/turn/model/latency stats from the persisted
        ``base_state.json``.

        The CLI persists the full (non-snapshot) ``ConversationStats`` — the
        same ``usage_to_metrics`` structure the old in-process backend read,
        just from JSON now. Streaming-only timings are unavailable in headless
        ``--json`` (no token-level callback), so ``ttft_ms`` stays 0 and
        ``streamed`` is False. We do have real per-call ``response_latencies``,
        so ``llm_ms`` is exact; ``decode_ms`` reuses it as a conservative
        throughput basis (it includes prefill/TTFT, so tok/sec is a lower
        bound rather than a fabricated number).
        """
        try:
            matches = sorted(state_dir.glob("conversations/*/base_state.json"))
            if not matches:
                logger.warning("no base_state.json under %s; stats unavailable", state_dir)
                return IterationStats()
            # A per-task persistence dir holds one conversation; if more, the
            # newest by name is the one we just ran.
            data = json.loads(matches[-1].read_text())
            usage_to_metrics = (data.get("stats") or {}).get("usage_to_metrics") or {}

            input_tokens = output_tokens = cache_read = cache_write = 0
            llm_secs = 0.0
            num_turns = 0
            max_input_tokens = 0
            models: list[str] = []

            for metrics in usage_to_metrics.values():
                acc = metrics.get("accumulated_token_usage") or {}
                input_tokens += int(acc.get("prompt_tokens", 0) or 0)
                output_tokens += int(acc.get("completion_tokens", 0) or 0)
                cache_read += int(acc.get("cache_read_tokens", 0) or 0)
                cache_write += int(acc.get("cache_write_tokens", 0) or 0)
                for rl in metrics.get("response_latencies") or []:
                    llm_secs += float(rl.get("latency", 0) or 0)
                    num_turns += 1
                for tu in metrics.get("token_usages") or []:
                    max_input_tokens = max(
                        max_input_tokens, int(tu.get("prompt_tokens", 0) or 0)
                    )
                mn = metrics.get("model_name")
                if mn and mn not in models:
                    models.append(mn)

            if not models and model_name:
                models = [model_name]
            llm_ms = int(llm_secs * 1000)

            return IterationStats(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_write,
                num_turns=num_turns,
                models=models,
                llm_ms=llm_ms,
                decode_ms=llm_ms,
                max_input_tokens=max_input_tokens,
                ttft_ms=0,
                streamed=False,
            )
        except Exception as e:
            logger.warning("Could not extract OH stats: %s", e)
            return IterationStats()
