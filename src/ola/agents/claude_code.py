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

from ola.agents.base import Agent, AgentResponse
from ola.stats import IterationStats

logger = logging.getLogger(__name__)

_BOOTSTRAP_FILES = (".credentials.json", ".claude.json", "settings.json")
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
    ) -> AgentResponse:
        try:
            return self._run_once(prompt, workdir, state_dir)
        except AuthenticationError:
            return AgentResponse(
                output="Authentication failed. Run `ola-sandbox <name>` to refresh credentials (copies ~/.claude/.credentials.json into sandbox).",
                success=False,
            )

    def _run_once(
        self, prompt: str, workdir: str, state_dir: str | None = None
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

        env = None
        if state_dir:
            sd = Path(state_dir)
            home_claude = Path.home() / ".claude"
            for fname in _BOOTSTRAP_FILES:
                src = home_claude / fname
                dst = sd / fname
                if src.exists() and not dst.exists():
                    shutil.copy2(src, dst)
                    logger.debug("Copied %s -> %s", src, dst)
            env = {**os.environ, "CLAUDE_CONFIG_DIR": str(sd)}
            logger.debug("CLAUDE_CONFIG_DIR=%s", sd)

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
            return self._stream(proc, prompt)
        except FileNotFoundError:
            logger.error("'claude' CLI not found")
            return AgentResponse(
                output="'claude' CLI not found. Install it first.",
                success=False,
            )

    def _stream(self, proc: subprocess.Popen, prompt: str) -> AgentResponse:
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
        models_seen: set[str] = set()
        result_data: dict | None = None
        max_input_tokens: int = 0

        # Per-turn timing via granular stream events
        total_ttft_ms: int = 0
        total_decode_ms: int = 0
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
                status.clear()
                proc.kill()
                proc.wait()
                raise AuthenticationError(
                    event.get("message", {}).get("content", [{}])[0].get("text", "")
                )

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
                    if err_code == "authentication_error":
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
                    # Turn complete — accumulate decode time
                    if token_start is not None:
                        total_decode_ms += int(
                            (time.monotonic() - token_start) * 1000
                        )
                    turn_start = None
                    token_start = None

            # --- Status display from assistant events (no timing) ---

            elif msg_type == "assistant" and "message" in event:
                for block in event["message"].get("content", []):
                    if block.get("type") == "text":
                        status.update(block["text"])
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        status.update(f"[tool] {name}")

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
                        datetime.fromtimestamp(resets_at, tz=timezone.utc)
                        .isoformat(timespec="seconds")
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
                if err_code == "authentication_error":
                    status.clear()
                    proc.kill()
                    proc.wait()
                    raise AuthenticationError(err_msg)
                api_error_type = err_code
                api_error_message = err_msg[:500] if err_msg else None
                logger.error(
                    "Anthropic API error: %s — %s", err_code, err_msg[:200]
                )

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
                datetime.fromtimestamp(resets_at, tz=timezone.utc)
                .isoformat(timespec="seconds")
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
                error_type=api_error_type,
                error_message=api_error_message,
            )
            output = result_data.get("result", "") if result_data else ""
            return AgentResponse(output=output, success=False, stats=stats)

        if result_data is None:
            stderr = proc.stderr.read() if proc.stderr else ""
            return AgentResponse(output=stderr, success=proc.returncode == 0)

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
                    llm_ms, api_ms_reported, delta, rel * 100,
                )

        return self._parse_result(
            result_data,
            models_seen,
            max_input_tokens=max_input_tokens,
            ttft_ms=total_ttft_ms,
            llm_ms=llm_ms,
        )

    def _parse_result(
        self,
        data: dict,
        models_seen: set[str],
        max_input_tokens: int = 0,
        ttft_ms: int = 0,
        llm_ms: int = 0,
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
            error_type=error_type,
            error_message=error_message,
        )

        return AgentResponse(output=output, success=success, stats=stats)
