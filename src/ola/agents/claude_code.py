import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from ola.agents.base import Agent, AgentResponse, ProgressCallback
from ola.events.schema import metrics_block
from ola.stats import IterationStats

logger = logging.getLogger(__name__)

_BOOTSTRAP_FILES = (".credentials.json", ".claude.json", "settings.json")
_ALWAYS_REFRESH = {".credentials.json"}
_STATUS_LINES = 3
_MAX_LINE_LEN = 72


class _StatusDisplay:
    """Rolling N-line in-place display on stderr."""

    def __init__(self, max_lines: int = _STATUS_LINES):
        self._max = max_lines
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._drawn = 0
        self._tty = sys.stderr.isatty()

    def update(self, text: str) -> None:
        """Push a new status line (truncated to _MAX_LINE_LEN)."""
        text = text.replace("\n", " ").strip()
        if not text:
            return
        if len(text) > _MAX_LINE_LEN:
            text = text[: _MAX_LINE_LEN - 1] + "…"
        self._lines.append(text)
        self._paint()

    def clear(self) -> None:
        """Erase the status area."""
        if not self._tty or self._drawn == 0:
            return
        out = sys.stderr
        for _ in range(self._drawn):
            out.write("\033[A\033[2K")
        out.flush()
        self._drawn = 0

    def _paint(self) -> None:
        if not self._tty:
            return
        out = sys.stderr
        # Move up to erase previous status
        for _ in range(self._drawn):
            out.write("\033[A\033[2K")
        # Write current lines
        for line in self._lines:
            out.write(f"  \033[2m{line}\033[0m\n")
        out.flush()
        self._drawn = len(self._lines)


class AuthenticationError(Exception):
    """Raised when Claude Code reports an authentication failure."""


def _is_self_hosted() -> bool:
    """True when LLM_BASE_URL is set — route cc to a self-hosted endpoint."""
    return bool(os.getenv("LLM_BASE_URL"))


def _self_hosted_env_overlay(model: str | None) -> dict[str, str]:
    """Build the ANTHROPIC_* env overlay for a self-hosted endpoint.

    Caller must verify LLM_BASE_URL is set before invoking.
    """
    from ola.agents.openhands import _resolve_localhost

    base_url = _resolve_localhost(os.environ["LLM_BASE_URL"])
    effective_model = model or os.getenv("LLM_MODEL", "")
    overlay = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": os.getenv("LLM_API_KEY", ""),
        "ANTHROPIC_MODEL": effective_model,
        "ANTHROPIC_SMALL_FAST_MODEL": effective_model,
    }
    # Claude Code asks for 32000 output tokens by default, which alone overflows
    # a small self-hosted context window (e.g. 32768). Let LLM_MAX_OUTPUT_TOKENS
    # — the same knob oh/cx read — cap it via CLAUDE_CODE_MAX_OUTPUT_TOKENS.
    max_output = os.getenv("LLM_MAX_OUTPUT_TOKENS")
    if max_output:
        overlay["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = max_output
    if os.getenv("LLM_SKIP_TLS_VERIFY", "").lower() == "true":
        overlay["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return overlay


class ClaudeCodeAgent(Agent):
    """Agent that delegates to the Claude Code CLI."""

    mnemonic = "cc"
    full_name = "Claude Code"
    state_dir_name = ".claude"

    def version(self) -> str:
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True
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
        try:
            return self._run_once(prompt, workdir, state_dir, on_progress=on_progress)
        except AuthenticationError:
            return AgentResponse(
                output="Authentication failed. Run `ola-sandbox <name>` to refresh credentials (copies ~/.claude/.credentials.json into sandbox).",
                success=False,
            )

    def _run_once(
        self,
        prompt: str,
        workdir: str,
        state_dir: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AgentResponse:
        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "-p",
        ]
        if self.model:
            cmd.extend(["--model", self.model])

        logger.debug("Running: %s", " ".join(cmd[:3]) + " ...")

        self_hosted = _is_self_hosted()

        env: dict[str, str] | None = None
        if state_dir:
            sd = Path(state_dir)
            if not self_hosted:
                home_claude = Path.home() / ".claude"
                for fname in _BOOTSTRAP_FILES:
                    src = home_claude / fname
                    dst = sd / fname
                    if src.exists() and (fname in _ALWAYS_REFRESH or not dst.exists()):
                        shutil.copy2(src, dst)
                        logger.debug("Copied %s -> %s", src, dst)
            env = {**os.environ, "CLAUDE_CONFIG_DIR": str(sd)}
            logger.debug("CLAUDE_CONFIG_DIR=%s", sd)

        if self_hosted:
            if env is None:
                env = {**os.environ}
            env.update(_self_hosted_env_overlay(self.model))
            logger.debug(
                "Self-hosted endpoint: ANTHROPIC_BASE_URL=%s",
                env["ANTHROPIC_BASE_URL"],
            )

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=workdir,
                env=env,
            )
            return self._stream(
                proc, prompt, self_hosted=self_hosted, on_progress=on_progress
            )
        except FileNotFoundError:
            logger.error("'claude' CLI not found")
            return AgentResponse(
                output="'claude' CLI not found. Install it first.",
                success=False,
            )

    def _stream(
        self,
        proc: subprocess.Popen,
        prompt: str,
        self_hosted: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> AgentResponse:
        """Read NDJSON stream, show rolling status, return final result.

        The CC CLI emits granular Anthropic API events wrapped inside
        ``stream_event`` envelopes (requires ``--include-partial-messages``):

            stream_event{message_start} ─> stream_event{content_block_start}
                                           ─> stream_event{message_delta}

        We unwrap the envelope and dispatch on the inner event type to get
        per-turn TTFT and decode timing.  The higher-level ``assistant``
        events are still used for the rolling status display.
        """
        proc.stdin.write(prompt)
        proc.stdin.close()

        status = _StatusDisplay()
        last_progress_ts: float = 0.0  # monotonic; throttle on_progress to 1/s

        def _emit_progress(text: str) -> None:
            nonlocal last_progress_ts
            if on_progress is None:
                return
            now = time.monotonic()
            if now - last_progress_ts < 1.0:
                return
            last_progress_ts = now
            # Throughput counters update at turn boundaries (message_delta);
            # until the first turn completes there is nothing to report.
            metrics = (
                metrics_block(
                    output_tokens=cum_output_tokens, decode_ms=total_decode_ms
                )
                if cum_output_tokens or total_decode_ms
                else None
            )
            try:
                on_progress(text, metrics)
            except Exception:
                logger.exception("on_progress callback raised; continuing")

        models_seen: set[str] = set()
        result_data: dict | None = None
        max_input_tokens: int = 0

        # Per-turn timing via granular stream events
        total_ttft_ms: int = 0
        total_decode_ms: int = 0
        cum_output_tokens: int = 0
        turn_start: float | None = None
        token_start: float | None = None

        # Rate-limit tracking
        rate_limit_hit: dict | None = None  # set on rejected w/o fallback
        rate_limit_warned: bool = False

        # API error tracking
        api_error_type: str | None = None
        api_error_message: str | None = None

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = event.get("type", "")

            if event.get("error") == "authentication_failed":
                err_msg = (
                    event.get("message", {}).get("content", [{}])[0].get("text", "")
                )
                if self_hosted:
                    api_error_type = "authentication_error"
                    api_error_message = err_msg[:500] if err_msg else None
                    logger.error("Self-hosted authentication error: %s", err_msg[:200])
                    continue
                status.clear()
                proc.kill()
                proc.wait()
                raise AuthenticationError(err_msg)

            # --- Unwrap stream_event envelope and dispatch ---

            if msg_type == "stream_event" and "event" in event:
                inner = event["event"]
                inner_type = inner.get("type", "")

                # Check for error before dispatching on inner_type
                if inner_type == "error" or (
                    "error" in inner and isinstance(inner["error"], dict)
                ):
                    err = inner.get("error", inner)
                    err_code = err.get("type", "api_error")
                    err_msg = err.get("message", "")
                    if err_code == "authentication_error" and not self_hosted:
                        status.clear()
                        proc.kill()
                        proc.wait()
                        raise AuthenticationError(err_msg)
                    api_error_type = err_code
                    api_error_message = err_msg[:500] if err_msg else None
                    logger.error(
                        "Anthropic API error in stream_event: %s — %s",
                        err_code,
                        err_msg[:200],
                    )

                elif inner_type == "message_start" and "message" in inner:
                    turn_start = time.monotonic()
                    token_start = None  # reset for new turn
                    model = inner["message"].get("model")
                    if model:
                        models_seen.add(model)
                    # Sum all three prompt-token buckets for max context
                    msg_usage = inner["message"].get("usage", {})
                    turn_input = (
                        msg_usage.get("input_tokens", 0)
                        + msg_usage.get("cache_creation_input_tokens", 0)
                        + msg_usage.get("cache_read_input_tokens", 0)
                    )
                    if turn_input > max_input_tokens:
                        max_input_tokens = turn_input

                elif inner_type == "content_block_start":
                    # First content block in this turn marks end of prefill
                    if turn_start is not None and token_start is None:
                        token_start = time.monotonic()
                        total_ttft_ms += int((token_start - turn_start) * 1000)

                elif inner_type == "message_delta":
                    # Turn complete — accumulate decode time and output tokens
                    # (message_delta usage.output_tokens is the message total)
                    if token_start is not None:
                        total_decode_ms += int((time.monotonic() - token_start) * 1000)
                    cum_output_tokens += (inner.get("usage") or {}).get(
                        "output_tokens", 0
                    )
                    turn_start = None
                    token_start = None

            # --- Status display from assistant events (no timing) ---

            elif msg_type == "assistant" and "message" in event:
                for block in event["message"].get("content", []):
                    if block.get("type") == "text":
                        text = block["text"]
                        status.update(text)
                        _emit_progress(text)
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        line = f"[tool] {name}"
                        status.update(line)
                        _emit_progress(line)

            # --- Rate-limit events from CC CLI ---

            elif msg_type == "rate_limit_event":
                info = event.get("rate_limit_info", {})
                rl_status = info.get("status", "")
                rl_type = info.get("rateLimitType", "unknown")
                utilization = info.get("utilization", 0)
                resets_at = info.get("resetsAt")
                fallback = info.get("unifiedRateLimitFallbackAvailable", False)

                if rl_status == "allowed_warning" and not rate_limit_warned:
                    rate_limit_warned = True
                    resets_str = (
                        datetime.fromtimestamp(resets_at, tz=timezone.utc).isoformat(
                            timespec="seconds"
                        )
                        if resets_at
                        else "unknown"
                    )
                    logger.warning(
                        "CC rate limit approaching: %s at %.0f%% utilization, "
                        "resets at %s",
                        rl_type,
                        utilization * 100,
                        resets_str,
                    )
                elif rl_status == "rejected" and fallback:
                    logger.info(
                        "CC rate limit rejected (%s) but fallback available — "
                        "CLI will use cheaper model",
                        rl_type,
                    )
                elif rl_status == "rejected" and not fallback:
                    logger.warning(
                        "CC rate limit rejected: %s, resets at %s",
                        rl_type,
                        resets_at,
                    )
                    rate_limit_hit = info

            # --- Top-level API error events ---

            elif msg_type == "error":
                err = event.get("error", event)
                err_code = err.get("type", "api_error")
                err_msg = err.get("message", "")
                if err_code == "authentication_error" and not self_hosted:
                    status.clear()
                    proc.kill()
                    proc.wait()
                    raise AuthenticationError(err_msg)
                api_error_type = err_code
                api_error_message = err_msg[:500] if err_msg else None
                logger.error("Anthropic API error: %s — %s", err_code, err_msg[:200])

            elif msg_type == "result":
                result_data = event

        status.clear()
        proc.wait()

        # Rate-limited with no successful result → return error with reset info
        if rate_limit_hit and (
            result_data is None or result_data.get("subtype") != "success"
        ):
            resets_at = rate_limit_hit.get("resetsAt")
            rl_type = rate_limit_hit.get("rateLimitType", "unknown")
            resets_iso = (
                datetime.fromtimestamp(resets_at, tz=timezone.utc).isoformat(
                    timespec="seconds"
                )
                if resets_at
                else "unknown"
            )
            llm_ms = total_ttft_ms + total_decode_ms
            stats = IterationStats(
                input_tokens=0,
                output_tokens=0,
                models=sorted(models_seen) if models_seen else [],
                max_input_tokens=max_input_tokens,
                ttft_ms=total_ttft_ms,
                llm_ms=llm_ms,
                decode_ms=total_decode_ms,
                error_type="rate_limited",
                error_message=f"{rl_type} limit hit, resets at {resets_iso}",
                rate_limit_resets_at=resets_at,
            )
            output = result_data.get("result", "") if result_data else ""
            return AgentResponse(output=output, success=False, stats=stats)

        # API error with no successful result → return error with stats
        if api_error_type and (
            result_data is None or result_data.get("subtype") != "success"
        ):
            llm_ms = total_ttft_ms + total_decode_ms
            stats = IterationStats(
                input_tokens=0,
                output_tokens=0,
                models=sorted(models_seen) if models_seen else [],
                max_input_tokens=max_input_tokens,
                ttft_ms=total_ttft_ms,
                llm_ms=llm_ms,
                decode_ms=total_decode_ms,
                error_type=api_error_type,
                error_message=api_error_message,
            )
            output = result_data.get("result", "") if result_data else ""
            return AgentResponse(output=output, success=False, stats=stats)

        if result_data is None:
            stderr = proc.stderr.read() if proc.stderr else ""
            llm_ms = total_ttft_ms + total_decode_ms
            stats = IterationStats(
                input_tokens=0,
                output_tokens=0,
                models=sorted(models_seen) if models_seen else [],
                max_input_tokens=max_input_tokens,
                ttft_ms=total_ttft_ms,
                llm_ms=llm_ms,
                decode_ms=total_decode_ms,
                error_type="no_result_event",
                error_message=(stderr[:500] if stderr else None),
            )
            return AgentResponse(
                output=stderr, success=proc.returncode == 0, stats=stats
            )

        llm_ms = total_ttft_ms + total_decode_ms

        # Warn if measured llm_ms diverges significantly from CLI-reported
        api_ms_reported = result_data.get("duration_api_ms", 0)
        if api_ms_reported > 0 and llm_ms > 0:
            delta = abs(llm_ms - api_ms_reported)
            rel = delta / api_ms_reported
            if delta > 1000 and rel > 0.20:
                logger.warning(
                    "CC llm_ms divergence: measured=%dms, result.duration_api_ms=%dms "
                    "(delta=%dms, %.0f%%) — possible CLI format change",
                    llm_ms,
                    api_ms_reported,
                    delta,
                    rel * 100,
                )

        return self._parse_result(
            result_data,
            models_seen,
            max_input_tokens=max_input_tokens,
            ttft_ms=total_ttft_ms,
            llm_ms=llm_ms,
            decode_ms=total_decode_ms,
        )

    def _parse_result(
        self,
        data: dict,
        models_seen: set[str],
        max_input_tokens: int = 0,
        ttft_ms: int = 0,
        llm_ms: int = 0,
        decode_ms: int = 0,
    ) -> AgentResponse:
        """Parse the final 'result' event from the stream."""
        output = data.get("result", "")
        success = data.get("subtype") == "success"
        usage = data.get("usage", {})

        input_tokens = usage.get("input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

        models = (
            sorted(models_seen) if models_seen else ([self.model] if self.model else [])
        )

        subtype = data.get("subtype", "")
        error_type: str | None = None
        error_message: str | None = None
        if subtype != "success":
            error_type = subtype or "unknown_error"
            error_message = output[:500] if output else None

        stats = IterationStats(
            input_tokens=input_tokens + cache_creation + cache_read,
            output_tokens=usage.get("output_tokens", 0),
            cache_creation_tokens=cache_creation,
            cache_read_tokens=cache_read,
            num_turns=data.get("num_turns", 0),
            models=models,
            max_input_tokens=max_input_tokens,
            ttft_ms=ttft_ms,
            llm_ms=llm_ms,
            decode_ms=decode_ms,
            error_type=error_type,
            error_message=error_message,
        )

        return AgentResponse(output=output, success=success, stats=stats)
