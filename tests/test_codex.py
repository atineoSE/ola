"""Tests for the CodexAgent."""

import json
import os
from unittest.mock import MagicMock, patch

from ola.agents.codex import CodexAgent, _build_config_toml


# ---------------------------------------------------------------------------
# Helpers — synthetic codex JSONL event stream
# ---------------------------------------------------------------------------


def _session_meta(id_: str = "sess-1", provider: str = "ola") -> str:
    return json.dumps(
        {
            "type": "session_meta",
            "payload": {"id": id_, "model_provider": provider},
        }
    )


def _turn_context(model: str = "gpt-4.1") -> str:
    return json.dumps(
        {
            "type": "turn_context",
            "payload": {"model": model},
        }
    )


def _response_item_assistant(text: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        }
    )


def _response_item_tool(name: str = "shell") -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": name},
        }
    )


def _token_count(
    total_input: int = 100,
    total_output: int = 50,
    total_cached: int = 20,
    last_input: int = 60,
) -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": total_input,
                        "output_tokens": total_output,
                        "cached_input_tokens": total_cached,
                    },
                    "last_token_usage": {"input_tokens": last_input},
                },
            },
        }
    )


def _task_complete(message: str = "All done.") -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": message},
        }
    )


def _make_proc(lines: list[str], stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = iter(line + "\n" for line in lines)
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = stderr
    proc.returncode = returncode
    proc.wait.return_value = returncode
    return proc


def _run_stream(lines: list[str], stderr: str = "", returncode: int = 0):
    proc = _make_proc(lines, stderr=stderr, returncode=returncode)
    return CodexAgent()._stream(proc)


# ---------------------------------------------------------------------------
# Stream parser tests
# ---------------------------------------------------------------------------


class TestStreamParser:
    def test_happy_path_returns_success_and_stats(self):
        lines = [
            _session_meta(),
            _turn_context(model="gpt-4.1"),
            _response_item_assistant("Working on it"),
            _token_count(
                total_input=100, total_output=50, total_cached=20, last_input=80
            ),
            _task_complete(message="All done."),
        ]
        resp = _run_stream(lines)
        assert resp.success is True
        assert resp.output == "All done."
        s = resp.stats
        assert s.input_tokens == 100
        assert s.output_tokens == 50
        assert s.cache_read_tokens == 20
        assert s.models == ["gpt-4.1"]
        assert s.max_input_tokens == 80
        assert s.streamed is False
        assert s.llm_ms == 0
        assert s.ttft_ms == 0
        assert s.error_type is None

    def test_token_count_overwrites_not_sums(self):
        lines = [
            _session_meta(),
            _turn_context(),
            _token_count(
                total_input=50, total_output=25, total_cached=5, last_input=50
            ),
            _token_count(
                total_input=200, total_output=80, total_cached=40, last_input=120
            ),
            _task_complete(),
        ]
        resp = _run_stream(lines)
        s = resp.stats
        assert s.input_tokens == 200  # latest event wins, not sum
        assert s.output_tokens == 80
        assert s.cache_read_tokens == 40

    def test_max_input_tokens_tracks_max_across_events(self):
        lines = [
            _session_meta(),
            _turn_context(),
            _token_count(last_input=100),
            _token_count(last_input=500),
            _token_count(last_input=200),
            _task_complete(),
        ]
        resp = _run_stream(lines)
        assert resp.stats.max_input_tokens == 500

    def test_turn_context_collects_models(self):
        lines = [
            _session_meta(),
            _turn_context(model="gpt-4.1"),
            _turn_context(model="gpt-4.1"),  # duplicate
            _turn_context(model="o3-mini"),
            _task_complete(),
        ]
        resp = _run_stream(lines)
        assert resp.stats.models == ["gpt-4.1", "o3-mini"]

    def test_malformed_json_lines_skipped(self):
        lines = [
            "not json {{{",
            "",
            _session_meta(),
            _token_count(total_input=10, total_output=5, total_cached=0, last_input=10),
            "another bad line",
            _task_complete(message="ok"),
        ]
        resp = _run_stream(lines)
        assert resp.success is True
        assert resp.stats.input_tokens == 10

    def test_no_task_complete_returns_failure(self):
        lines = [
            _session_meta(),
            _turn_context(),
            _token_count(),
            # No task_complete
        ]
        resp = _run_stream(lines, stderr="codex crashed", returncode=1)
        assert resp.success is False
        assert resp.stats.error_type == "no_task_complete"
        assert resp.stats.error_message == "codex crashed"

    def test_no_task_complete_truncates_long_stderr(self):
        lines = [_session_meta()]
        long_err = "x" * 1000
        resp = _run_stream(lines, stderr=long_err, returncode=1)
        assert resp.stats.error_type == "no_task_complete"
        assert len(resp.stats.error_message) == 500

    def test_empty_stream_no_task_complete(self):
        resp = _run_stream([], returncode=1)
        assert resp.success is False
        assert resp.stats.error_type == "no_task_complete"

    def test_response_item_tool_call_does_not_crash(self):
        lines = [
            _session_meta(),
            _response_item_tool(name="shell"),
            _response_item_assistant("hello"),
            _task_complete(),
        ]
        resp = _run_stream(lines)
        assert resp.success is True


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
                line + "\n" for line in [_session_meta(), _task_complete(message="ok")]
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
