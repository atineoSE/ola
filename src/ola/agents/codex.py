import json
import logging
import os
import subprocess
import sys
from collections import deque
from pathlib import Path

from ola.agents.base import Agent, AgentResponse
from ola.agents.openhands import _resolve_localhost
from ola.stats import IterationStats

logger = logging.getLogger(__name__)

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
        text = text.replace("\n", " ").strip()
        if not text:
            return
        if len(text) > _MAX_LINE_LEN:
            text = text[: _MAX_LINE_LEN - 1] + "…"
        self._lines.append(text)
        self._paint()

    def clear(self) -> None:
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
        for _ in range(self._drawn):
            out.write("\033[A\033[2K")
        for line in self._lines:
            out.write(f"  \033[2m{line}\033[0m\n")
        out.flush()
        self._drawn = len(self._lines)


def _build_config_toml(model: str, base_url: str | None, wire_api: str) -> str:
    """Render the codex config.toml that points at the ola provider."""
    lines = ['model_provider = "ola"']
    if model:
        lines.append(f'model = "{model}"')
    lines.append("")
    lines.append("[model_providers.ola]")
    lines.append('name = "ola"')
    if base_url:
        lines.append(f'base_url = "{base_url}"')
    lines.append('env_key = "LLM_API_KEY"')
    lines.append(f'wire_api = "{wire_api}"')
    lines.append("")
    return "\n".join(lines)


class CodexAgent(Agent):
    """Agent that delegates to the Codex CLI."""

    mnemonic = "cx"
    full_name = "Codex"
    state_dir_name = ".codex"

    def version(self) -> str:
        try:
            result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True
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
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            logger.error("LLM_API_KEY environment variable is not set")
            return AgentResponse(
                output="LLM_API_KEY environment variable is not set.",
                success=False,
            )

        if not state_dir:
            return AgentResponse(
                output="state_dir is required for CodexAgent.",
                success=False,
            )

        model_name = self.model or os.getenv("LLM_MODEL") or ""
        base_url = os.getenv("LLM_BASE_URL") or None
        if base_url:
            base_url = _resolve_localhost(base_url)
        wire_api = os.getenv("LLM_WIRE_API") or "responses"

        sd = Path(state_dir)
        sd.mkdir(parents=True, exist_ok=True)
        config_path = sd / "config.toml"
        config_path.write_text(_build_config_toml(model_name, base_url, wire_api))
        last_path = sd / "last.txt"

        cmd = [
            "codex",
            "exec",
            "--json",
            "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            workdir,
            "-o",
            str(last_path),
        ]
        if self.model:
            cmd.extend(["-m", self.model])
        cmd.append(prompt)

        env = {**os.environ, "CODEX_HOME": str(sd)}

        logger.debug("Running: %s ...", " ".join(cmd[:4]))
        logger.debug("CODEX_HOME=%s", sd)

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
            logger.error("'codex' CLI not found")
            return AgentResponse(
                output=(
                    "'codex' CLI not found. "
                    "Install with `npm install -g @openai/codex`."
                ),
                success=False,
            )

        return self._stream(proc)

    def _stream(self, proc: subprocess.Popen) -> AgentResponse:
        status = _StatusDisplay()
        models_seen: set[str] = set()
        # The codex v0.130.0 event stream doesn't surface the effective model;
        # fall back to whatever we configured.
        configured_model = self.model or os.getenv("LLM_MODEL") or ""
        if configured_model:
            models_seen.add(configured_model)
        last_agent_message: str | None = None
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        max_input_tokens = 0
        turn_completed = False
        turn_error: str | None = None

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            evt_type = event.get("type", "")

            if evt_type == "thread.started":
                logger.debug("Codex thread started: id=%s", event.get("thread_id"))

            elif evt_type == "turn.started":
                pass

            elif evt_type == "item.started":
                self._render_item_status(event.get("item") or {}, status)

            elif evt_type == "item.completed":
                item = event.get("item") or {}
                self._render_item_status(item, status)
                if item.get("type") == "agent_message":
                    txt = item.get("text")
                    if txt:
                        last_agent_message = txt

            elif evt_type == "turn.completed":
                turn_completed = True
                usage = event.get("usage") or {}
                in_tok = int(usage.get("input_tokens", 0) or 0)
                out_tok = int(usage.get("output_tokens", 0) or 0)
                cached = int(usage.get("cached_input_tokens", 0) or 0)
                input_tokens += in_tok
                output_tokens += out_tok
                cache_read_tokens += cached
                if in_tok > max_input_tokens:
                    max_input_tokens = in_tok

            elif evt_type == "turn.failed":
                err = event.get("error") or {}
                turn_error = err.get("message") if isinstance(err, dict) else str(err)

            elif evt_type == "error":
                turn_error = event.get("message") or turn_error

        status.clear()
        proc.wait()

        success = turn_completed and turn_error is None
        if not success:
            stderr = proc.stderr.read() if proc.stderr else ""
            error_message = turn_error or (stderr[:500] if stderr else None)
            stats = IterationStats(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                models=sorted(models_seen) if models_seen else [],
                max_input_tokens=max_input_tokens,
                ttft_ms=0,
                llm_ms=0,
                streamed=False,
                error_type="turn_failed" if turn_error else "no_task_complete",
                error_message=error_message,
            )
            return AgentResponse(
                output=last_agent_message or turn_error or stderr,
                success=False,
                stats=stats,
            )

        stats = IterationStats(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            models=sorted(models_seen) if models_seen else [],
            max_input_tokens=max_input_tokens,
            ttft_ms=0,
            llm_ms=0,
            streamed=False,
        )
        return AgentResponse(
            output=last_agent_message or "",
            success=True,
            stats=stats,
        )

    def _render_item_status(self, item: dict, status: _StatusDisplay) -> None:
        """Push a status line for an assistant message or tool call."""
        if not isinstance(item, dict):
            return
        item_type = item.get("type", "")
        if item_type == "agent_message":
            text = item.get("text") or ""
            if text:
                status.update(text)
        elif item_type == "command_execution":
            cmd = item.get("command") or "?"
            status.update(f"[cmd] {cmd}")
        elif item_type:
            status.update(f"[{item_type}]")
