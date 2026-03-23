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

_CREDENTIAL_FILES = (".credentials.json",)
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

    def _stream(
        self, proc: subprocess.Popen, prompt: str, timeout: int = 600
    ) -> AgentResponse:
        """Read NDJSON stream, show rolling status, return final result."""
        proc.stdin.write(prompt)
        proc.stdin.close()

        status = _StatusDisplay()
        result_data: dict | None = None
        deadline = time.monotonic() + timeout

        for line in proc.stdout:
            if time.monotonic() > deadline:
                status.clear()
                proc.kill()
                proc.wait()
                logger.error("Claude Code timed out after %ds", timeout)
                return AgentResponse(
                    output=f"Claude Code timed out after {timeout}s",
                    success=False,
                )

            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = event.get("type", "")

            if msg_type == "assistant" and "message" in event:
                for block in event["message"].get("content", []):
                    if block.get("type") == "text":
                        status.update(block["text"])
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        status.update(f"[tool] {name}")

            if msg_type == "result":
                result_data = event

        status.clear()
        proc.wait()

        if result_data is None:
            stderr = proc.stderr.read() if proc.stderr else ""
            return AgentResponse(output=stderr, success=proc.returncode == 0)

        return self._parse_result(result_data)

    def _parse_result(self, data: dict) -> AgentResponse:
        """Parse the final 'result' event from the stream."""
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
