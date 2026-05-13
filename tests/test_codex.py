"""Tests for the CodexAgent."""

import json
import os
from unittest.mock import MagicMock, patch

from ola.agents.codex import CodexAgent, _build_config_toml


# ---------------------------------------------------------------------------
# Helpers — synthetic codex JSONL event stream (v0.130.0 format)
# ---------------------------------------------------------------------------


def _thread_started(thread_id: str = "thr-1") -> str:
    return json.dumps({"type": "thread.started", "thread_id": thread_id})


def _turn_started() -> str:
    return json.dumps({"type": "turn.started"})


def _item_agent_message(text: str, item_id: str = "item_msg") -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {"id": item_id, "type": "agent_message", "text": text},
        }
    )


def _item_command_started(command: str = "ls", item_id: str = "item_cmd") -> str:
    return json.dumps(
        {
            "type": "item.started",
            "item": {
                "id": item_id,
                "type": "command_execution",
                "command": command,
                "aggregated_output": "",
                "exit_code": None,
                "status": "in_progress",
            },
        }
    )


def _item_command_completed(
    command: str = "ls",
    item_id: str = "item_cmd",
    output: str = "",
    exit_code: int = 0,
) -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": item_id,
                "type": "command_execution",
                "command": command,
                "aggregated_output": output,
                "exit_code": exit_code,
                "status": "completed",
            },
        }
    )


def _turn_completed(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cached_input_tokens: int = 20,
    reasoning_output_tokens: int = 0,
) -> str:
    return json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
            },
        }
    )


def _turn_failed(message: str = "boom") -> str:
    return json.dumps({"type": "turn.failed", "error": {"message": message}})


def _make_proc(lines: list[str], stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = iter(line + "\n" for line in lines)
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = stderr
    proc.returncode = returncode
    proc.wait.return_value = returncode
    return proc


def _run_stream(
    lines: list[str], stderr: str = "", returncode: int = 0, model: str | None = None
):
    proc = _make_proc(lines, stderr=stderr, returncode=returncode)
    agent = CodexAgent(model=model)
    return agent._stream(proc)


# ---------------------------------------------------------------------------
# Stream parser tests
# ---------------------------------------------------------------------------


class TestStreamParser:
    def test_happy_path_returns_success_and_stats(self):
        lines = [
            _thread_started(),
            _turn_started(),
            _item_agent_message("All done."),
            _turn_completed(input_tokens=100, output_tokens=50, cached_input_tokens=20),
        ]
        resp = _run_stream(lines, model="gpt-4.1")
        assert resp.success is True
        assert resp.output == "All done."
        s = resp.stats
        assert s.input_tokens == 100
        assert s.output_tokens == 50
        assert s.cache_read_tokens == 20
        assert s.models == ["gpt-4.1"]
        assert s.max_input_tokens == 100
        assert s.streamed is False
        assert s.llm_ms == 0
        assert s.ttft_ms == 0
        assert s.error_type is None

    def test_multiple_turns_sum_usage(self):
        lines = [
            _thread_started(),
            _turn_started(),
            _item_agent_message("partial", item_id="m1"),
            _turn_completed(input_tokens=50, output_tokens=25, cached_input_tokens=5),
            _turn_started(),
            _item_agent_message("final", item_id="m2"),
            _turn_completed(input_tokens=200, output_tokens=80, cached_input_tokens=40),
        ]
        resp = _run_stream(lines, model="gpt-4.1")
        s = resp.stats
        assert s.input_tokens == 250  # summed across turns
        assert s.output_tokens == 105
        assert s.cache_read_tokens == 45
        assert s.max_input_tokens == 200  # largest single-turn input
        assert resp.output == "final"  # latest agent_message wins

    def test_models_seeded_from_configured_model(self):
        lines = [_thread_started(), _turn_completed()]
        resp = _run_stream(lines, model="o3-mini")
        assert resp.stats.models == ["o3-mini"]

    def test_malformed_json_lines_skipped(self):
        lines = [
            "not json {{{",
            "",
            _thread_started(),
            _turn_started(),
            "another bad line",
            _item_agent_message("ok"),
            _turn_completed(input_tokens=10, output_tokens=5, cached_input_tokens=0),
        ]
        resp = _run_stream(lines, model="gpt-4.1")
        assert resp.success is True
        assert resp.stats.input_tokens == 10
        assert resp.output == "ok"

    def test_no_turn_completed_returns_failure(self):
        lines = [
            _thread_started(),
            _turn_started(),
            _item_agent_message("partial"),
            # No turn.completed
        ]
        resp = _run_stream(lines, stderr="codex crashed", returncode=1, model="m")
        assert resp.success is False
        assert resp.stats.error_type == "no_task_complete"
        assert resp.stats.error_message == "codex crashed"

    def test_no_turn_completed_truncates_long_stderr(self):
        lines = [_thread_started()]
        long_err = "x" * 1000
        resp = _run_stream(lines, stderr=long_err, returncode=1, model="m")
        assert resp.stats.error_type == "no_task_complete"
        assert len(resp.stats.error_message) == 500

    def test_empty_stream_no_task_complete(self):
        resp = _run_stream([], returncode=1, model="m")
        assert resp.success is False
        assert resp.stats.error_type == "no_task_complete"

    def test_turn_failed_marks_failure(self):
        lines = [
            _thread_started(),
            _turn_started(),
            _item_agent_message("trying"),
            _turn_failed("provider rejected request"),
        ]
        resp = _run_stream(lines, model="m")
        assert resp.success is False
        assert resp.stats.error_type == "turn_failed"
        assert "provider rejected" in (resp.stats.error_message or "")

    def test_command_execution_items_do_not_crash(self):
        lines = [
            _thread_started(),
            _turn_started(),
            _item_command_started("ls", item_id="c1"),
            _item_command_completed("ls", item_id="c1", output="file\n"),
            _item_agent_message("done"),
            _turn_completed(),
        ]
        resp = _run_stream(lines, model="m")
        assert resp.success is True
        assert resp.output == "done"


# ---------------------------------------------------------------------------
# Error paths in run()
# ---------------------------------------------------------------------------


class TestRunErrorPaths:
    def test_missing_api_key(self, tmp_path):
        agent = CodexAgent()
        with patch.dict(os.environ, {}, clear=True):
            resp = agent.run(
                prompt="hello",
                workdir=str(tmp_path),
                state_dir=str(tmp_path / ".codex"),
            )
        assert resp.success is False
        assert "LLM_API_KEY" in resp.output

    def test_missing_state_dir(self, tmp_path):
        agent = CodexAgent()
        with patch.dict(os.environ, {"LLM_API_KEY": "secret"}, clear=True):
            resp = agent.run(prompt="hello", workdir=str(tmp_path), state_dir=None)
        assert resp.success is False
        assert "state_dir" in resp.output

    def test_codex_binary_missing(self, tmp_path):
        agent = CodexAgent()
        env = {"LLM_API_KEY": "secret", "LLM_MODEL": "gpt-4.1"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("ola.agents.codex.subprocess.Popen", side_effect=FileNotFoundError),
        ):
            resp = agent.run(
                prompt="hello",
                workdir=str(tmp_path),
                state_dir=str(tmp_path / ".codex"),
            )
        assert resp.success is False
        assert "codex" in resp.output.lower()
        assert "install" in resp.output.lower()


# ---------------------------------------------------------------------------
# Config / env wiring via run()
# ---------------------------------------------------------------------------


class TestConfigGeneration:
    def _spawn_capture(self, tmp_path, env: dict, model: str | None = None):
        """Invoke ``CodexAgent.run`` with a stubbed Popen; return the captured
        Popen kwargs together with the generated config path."""
        agent = CodexAgent(model=model)
        state_dir = tmp_path / ".codex"
        captured: dict = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            proc = MagicMock()
            proc.stdout = iter(
                line + "\n"
                for line in [
                    _thread_started(),
                    _turn_started(),
                    _item_agent_message("ok"),
                    _turn_completed(),
                ]
            )
            proc.stderr = MagicMock()
            proc.stderr.read.return_value = ""
            proc.returncode = 0
            proc.wait.return_value = 0
            return proc

        with (
            patch.dict(os.environ, env, clear=True),
            patch("ola.agents.codex.subprocess.Popen", side_effect=fake_popen),
        ):
            resp = agent.run(
                prompt="hi",
                workdir=str(tmp_path),
                state_dir=str(state_dir),
            )

        return resp, captured, state_dir

    def test_config_toml_written(self, tmp_path):
        env = {
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "gpt-4.1",
            "LLM_BASE_URL": "https://example.com/v1",
        }
        resp, captured, state_dir = self._spawn_capture(tmp_path, env)
        assert resp.success is True
        cfg = (state_dir / "config.toml").read_text()
        assert 'env_key = "LLM_API_KEY"' in cfg
        assert 'base_url = "https://example.com/v1"' in cfg
        assert 'wire_api = "responses"' in cfg
        assert 'model_provider = "ola"' in cfg
        assert 'model = "gpt-4.1"' in cfg

    def test_wire_api_override(self, tmp_path):
        env = {
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "gpt-4.1",
            "LLM_BASE_URL": "https://example.com/v1",
            "LLM_WIRE_API": "chat",
        }
        resp, _captured, state_dir = self._spawn_capture(tmp_path, env)
        assert resp.success is True
        assert 'wire_api = "chat"' in (state_dir / "config.toml").read_text()

    def test_codex_home_env_set(self, tmp_path):
        env = {
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "gpt-4.1",
            "LLM_BASE_URL": "https://example.com/v1",
        }
        _resp, captured, state_dir = self._spawn_capture(tmp_path, env)
        popen_env = captured["kwargs"]["env"]
        assert popen_env["CODEX_HOME"] == str(state_dir)
        assert popen_env["LLM_API_KEY"] == "secret"

    def test_localhost_rewrite_in_sandbox(self, tmp_path):
        env = {
            "SANDBOX": "1",
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "gpt-4.1",
            "LLM_BASE_URL": "http://localhost:11434/v1",
        }
        _resp, _captured, state_dir = self._spawn_capture(tmp_path, env)
        cfg = (state_dir / "config.toml").read_text()
        assert "host.docker.internal" in cfg
        assert "localhost" not in cfg

    def test_localhost_not_rewritten_outside_sandbox(self, tmp_path):
        env = {
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "gpt-4.1",
            "LLM_BASE_URL": "http://localhost:11434/v1",
        }
        _resp, _captured, state_dir = self._spawn_capture(tmp_path, env)
        cfg = (state_dir / "config.toml").read_text()
        assert "localhost" in cfg

    def test_model_flag_added_when_self_model_set(self, tmp_path):
        env = {
            "LLM_API_KEY": "secret",
            "LLM_BASE_URL": "https://example.com/v1",
        }
        _resp, captured, _state = self._spawn_capture(tmp_path, env, model="o3-mini")
        cmd = captured["cmd"]
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "o3-mini"

    def test_command_shape(self, tmp_path):
        env = {
            "LLM_API_KEY": "secret",
            "LLM_MODEL": "gpt-4.1",
            "LLM_BASE_URL": "https://example.com/v1",
        }
        _resp, captured, state_dir = self._spawn_capture(tmp_path, env)
        cmd = captured["cmd"]
        assert cmd[:4] == ["codex", "exec", "--json", "--ephemeral"]
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "-C" in cmd
        assert "-o" in cmd
        # -o points at <state_dir>/last.txt
        assert cmd[cmd.index("-o") + 1] == str(state_dir / "last.txt")
        # prompt is the final arg
        assert cmd[-1] == "hi"


# ---------------------------------------------------------------------------
# Config builder unit tests
# ---------------------------------------------------------------------------


class TestBuildConfigToml:
    def test_includes_all_keys(self):
        cfg = _build_config_toml(
            model="gpt-4.1",
            base_url="https://example.com/v1",
            wire_api="responses",
        )
        assert 'model_provider = "ola"' in cfg
        assert 'model = "gpt-4.1"' in cfg
        assert "[model_providers.ola]" in cfg
        assert 'env_key = "LLM_API_KEY"' in cfg
        assert 'base_url = "https://example.com/v1"' in cfg
        assert 'wire_api = "responses"' in cfg

    def test_omits_model_when_empty(self):
        cfg = _build_config_toml(
            model="", base_url="https://example.com/v1", wire_api="responses"
        )
        assert "model =" not in cfg or "model_provider" in cfg
        # Specifically: no top-level `model = "..."` line
        for line in cfg.splitlines():
            assert not line.startswith("model = ")

    def test_omits_base_url_when_none(self):
        cfg = _build_config_toml(model="x", base_url=None, wire_api="responses")
        assert "base_url" not in cfg
