import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import deque
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
                output="Authentication failed. Run `sbx secret set -g anthropic` on the host.",
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

        Processes three granular Anthropic API streaming event types to get
        per-turn TTFT and decode timing:

            message_start ─[prefill]─> content_block_start ─[decode]─> message_delta
                 │                            │                             │
            turn begins              first token generated            turn ends

        The higher-level ``assistant`` events are still processed for the
        rolling status display but no longer carry timing responsibility.
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

            # --- Granular timing events ---

            if msg_type == "message_start" and "message" in event:
                turn_start = time.monotonic()
                token_start = None  # reset for new turn
                model = event["message"].get("model")
                if model:
                    models_seen.add(model)
                # Track per-turn input tokens for max context size
                msg_usage = event["message"].get("usage", {})
                turn_input = msg_usage.get("input_tokens", 0)
                if turn_input > max_input_tokens:
                    max_input_tokens = turn_input

            elif msg_type == "content_block_start":
                # First content block in this turn marks end of prefill
                if turn_start is not None and token_start is None:
                    token_start = time.monotonic()
                    total_ttft_ms += int((token_start - turn_start) * 1000)

            elif msg_type == "message_delta":
                # Turn complete — accumulate decode time
                if token_start is not None:
                    total_decode_ms += int((time.monotonic() - token_start) * 1000)
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

            elif msg_type == "result":
                result_data = event

        status.clear()
        proc.wait()

        if result_data is None:
            stderr = proc.stderr.read() if proc.stderr else ""
            return AgentResponse(output=stderr, success=proc.returncode == 0)

        llm_ms = total_ttft_ms + total_decode_ms
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
        )

        return AgentResponse(output=output, success=success, stats=stats)
