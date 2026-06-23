"""Tests for the OpenHands CLI subprocess backend and sandbox utilities."""

import inspect
import json
import os
from unittest.mock import patch

import pytest

from ola.agents.openhands import (
    OpenHandsAgent,
    _agent_message_text,
    _build_agent_settings,
    _build_llm_config,
    _event_text,
)
from ola.sandbox import is_sandbox


class TestRunSignature:
    """Verify run() accepts the on_progress callback parameter."""

    def test_run_has_on_progress_parameter(self):
        sig = inspect.signature(OpenHandsAgent.run)
        assert "on_progress" in sig.parameters
        assert sig.parameters["on_progress"].default is None

    def test_run_missing_api_key(self, tmp_path):
        agent = OpenHandsAgent()
        with patch.dict(os.environ, {}, clear=True):
            resp = agent.run(
                prompt="hi",
                workdir=str(tmp_path),
                state_dir=str(tmp_path / ".openhands"),
                on_progress=lambda msg: None,
            )
        assert resp.success is False
        assert "LLM_API_KEY" in resp.output

    def test_run_requires_state_dir(self, tmp_path):
        agent = OpenHandsAgent()
        with patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_MODEL": "m"}):
            resp = agent.run(prompt="hi", workdir=str(tmp_path), state_dir=None)
        assert resp.success is False
        assert "state_dir" in resp.output

    def test_run_requires_model(self, tmp_path):
        agent = OpenHandsAgent()
        with patch.dict(os.environ, {"LLM_API_KEY": "k"}, clear=True):
            resp = agent.run(
                prompt="hi",
                workdir=str(tmp_path),
                state_dir=str(tmp_path / ".openhands"),
            )
        assert resp.success is False
        assert "model" in resp.output.lower()


class TestBuildAgentSettings:
    """The agent_settings.json the headless CLI loads from the persistence dir."""

    def test_required_fields(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = _build_agent_settings("claude-x", "sk-real", "https://h/v1")
        assert settings["kind"] == "Agent"
        assert settings["llm"]["model"] == "claude-x"
        assert settings["llm"]["api_key"] == "sk-real"  # plaintext, per-task private dir
        assert settings["llm"]["base_url"] == "https://h/v1"
        assert settings["llm"]["usage_id"] == "agent"

    def test_condenser_present_with_own_usage_id(self):
        """A condenser is included so long-horizon runs get summarization
        (the CLI only wires one if it is already in the persisted agent)."""
        with patch.dict(os.environ, {}, clear=True):
            settings = _build_agent_settings("m", "k", None)
        assert settings["condenser"]["kind"] == "LLMSummarizingCondenser"
        assert settings["condenser"]["llm"]["usage_id"] == "condenser"

    def test_no_base_url_omitted(self):
        with patch.dict(os.environ, {}, clear=True):
            llm = _build_llm_config("m", "k", None, "agent")
        assert "base_url" not in llm

    def test_optional_env_knobs_typed(self):
        env = {
            "LLM_TEMPERATURE": "0.2",
            "LLM_MAX_OUTPUT_TOKENS": "4096",
            "LLM_NUM_RETRIES": "0",
            "LLM_TOP_P": "0.9",
            "LLM_ENABLE_ENCRYPTED_REASONING": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            llm = _build_llm_config("m", "k", None, "agent")
        assert llm["temperature"] == 0.2
        assert llm["max_output_tokens"] == 4096
        assert llm["num_retries"] == 0
        assert llm["top_p"] == 0.9
        assert llm["enable_encrypted_reasoning"] is True

    def test_unset_knobs_absent(self):
        with patch.dict(os.environ, {}, clear=True):
            llm = _build_llm_config("m", "k", None, "agent")
        assert "temperature" not in llm
        assert "max_output_tokens" not in llm


class TestEventText:
    """Status-line and final-message extraction from parsed event dicts."""

    def test_agent_message(self):
        ev = {
            "kind": "MessageEvent",
            "source": "agent",
            "llm_message": {"content": [{"type": "text", "text": "hello world"}]},
        }
        assert _event_text(ev) == "hello world"
        assert _agent_message_text(ev) == "hello world"

    def test_user_message_ignored(self):
        ev = {
            "kind": "MessageEvent",
            "source": "user",
            "llm_message": {"content": [{"type": "text", "text": "hi"}]},
        }
        assert _event_text(ev) is None
        assert _agent_message_text(ev) is None

    def test_action_event(self):
        ev = {"kind": "ActionEvent", "tool_name": "terminal", "summary": "ls -la"}
        assert _event_text(ev) == "[terminal] ls -la"
        # an action is not a final agent message
        assert _agent_message_text(ev) is None

    def test_other_event(self):
        assert _event_text({"kind": "ObservationEvent"}) is None


class TestIterEvents:
    """The --JSON Event-- multi-block stdout parser."""

    def _parse(self, text: str):
        return list(OpenHandsAgent._iter_events(iter(text.splitlines(keepends=True))))

    def test_two_pretty_blocks(self):
        stream = (
            "--JSON Event--\n"
            '{\n  "kind": "MessageEvent",\n  "source": "agent"\n}\n'
            "--JSON Event--\n"
            '{\n  "kind": "ActionEvent",\n  "tool_name": "terminal"\n}\n'
        )
        events = self._parse(stream)
        assert [e["kind"] for e in events] == ["MessageEvent", "ActionEvent"]

    def test_trailing_rich_console_after_last_event(self):
        """The final block is followed by Rich console output in the same
        block; raw_decode must still recover the JSON object (regression:
        json.loads would choke and drop the last — often error — event)."""
        stream = (
            "--JSON Event--\n"
            '{\n  "kind": "ConversationErrorEvent",\n  "code": "Boom",\n'
            '  "detail": "exploded"\n}\n'
            "\n───── CONVERSATION SUMMARY ─────\n"
            "Goodbye! 👋\n"
            "Conversation ID: abc123\n"
        )
        events = self._parse(stream)
        assert len(events) == 1
        assert events[0]["kind"] == "ConversationErrorEvent"
        assert events[0]["code"] == "Boom"

    def test_non_json_leading_lines_skipped(self):
        stream = "some banner line\n--JSON Event--\n{\n  \"kind\": \"X\"\n}\n"
        events = self._parse(stream)
        assert [e["kind"] for e in events] == ["X"]


def _base_state(usage_to_metrics: dict) -> dict:
    return {"stats": {"usage_to_metrics": usage_to_metrics}}


def _metrics(model, prompt, completion, cache_read=0, cache_write=0, latencies=None, usages=None):
    return {
        "model_name": model,
        "accumulated_token_usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
        },
        "response_latencies": [
            {"latency": v, "response_id": f"r{i}", "model": model}
            for i, v in enumerate(latencies or [])
        ],
        "token_usages": [{"prompt_tokens": p} for p in (usages or [])],
    }


class TestExtractStats:
    """_extract_stats reads the persisted base_state.json post-hoc."""

    def _write(self, tmp_path, base_state: dict):
        conv = tmp_path / "conversations" / "deadbeef"
        conv.mkdir(parents=True)
        (conv / "base_state.json").write_text(json.dumps(base_state))

    def _extract(self, tmp_path, model="test-model"):
        agent = OpenHandsAgent()
        return agent._extract_stats(tmp_path, model)

    def test_single_metric(self, tmp_path):
        self._write(
            tmp_path,
            _base_state(
                {
                    "agent": _metrics(
                        "claude-sonnet", 1000, 200, cache_read=500, cache_write=100,
                        latencies=[2.5], usages=[1000],
                    )
                }
            ),
        )
        stats = self._extract(tmp_path)
        assert stats.input_tokens == 1000
        assert stats.output_tokens == 200
        assert stats.cache_read_tokens == 500
        assert stats.cache_creation_tokens == 100
        assert stats.num_turns == 1
        assert stats.models == ["claude-sonnet"]
        assert stats.llm_ms == 2500
        assert stats.decode_ms == 2500  # decode reuses llm_ms (no token-level timing)
        assert stats.max_input_tokens == 1000
        assert stats.ttft_ms == 0
        assert stats.streamed is False

    def test_multi_usage_aggregation(self, tmp_path):
        self._write(
            tmp_path,
            _base_state(
                {
                    "agent": _metrics("model-a", 1000, 100, latencies=[1.0, 0.5], usages=[800, 1000]),
                    "condenser": _metrics("model-a", 2000, 300, latencies=[2.0], usages=[2000]),
                }
            ),
        )
        stats = self._extract(tmp_path)
        assert stats.input_tokens == 3000
        assert stats.output_tokens == 400
        assert stats.num_turns == 3
        assert stats.llm_ms == 3500
        assert stats.max_input_tokens == 2000
        assert stats.models == ["model-a"]

    def test_model_fallback_when_metrics_blank(self, tmp_path):
        self._write(tmp_path, _base_state({"agent": _metrics("", 10, 5)}))
        stats = self._extract(tmp_path, model="configured-model")
        assert stats.models == ["configured-model"]

    def test_missing_base_state_is_empty_stats(self, tmp_path):
        stats = self._extract(tmp_path)
        assert stats.input_tokens == 0
        assert stats.num_turns == 0


class TestIsSandbox:
    def test_sandbox_env_set(self):
        with patch.dict(os.environ, {"SANDBOX": "1"}):
            assert is_sandbox() is True

    def test_sandbox_env_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            assert is_sandbox() is False

    def test_sandbox_env_zero(self):
        with patch.dict(os.environ, {"SANDBOX": "0"}):
            assert is_sandbox() is False


class TestResolveLocalhost:
    @pytest.fixture(autouse=True)
    def _import(self):
        from ola.agents.openhands import _resolve_localhost

        self._resolve = _resolve_localhost

    def test_remote_url_unchanged(self):
        with patch.dict(os.environ, {"SANDBOX": "1"}):
            url = "https://api.example.com:8080/v1"
            assert self._resolve(url) == url

    def test_localhost_in_sandbox(self):
        with patch.dict(os.environ, {"SANDBOX": "1"}):
            assert (
                self._resolve("http://localhost:11434/v1")
                == "http://host.docker.internal:11434/v1"
            )

    def test_127_in_sandbox(self):
        with patch.dict(os.environ, {"SANDBOX": "1"}):
            assert (
                self._resolve("http://127.0.0.1:8080/v1")
                == "http://host.docker.internal:8080/v1"
            )

    def test_localhost_outside_sandbox(self):
        with patch.dict(os.environ, {}, clear=True):
            url = "http://localhost:11434/v1"
            assert self._resolve(url) == url
