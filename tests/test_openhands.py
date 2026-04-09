"""Tests for openhands agent helpers and sandbox utilities."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ola.agents.openhands import OpenHandsAgent, _TTFTTracker
from ola.sandbox import is_sandbox
from ola.stats import IterationStats


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

    def test_remote_url_outside_sandbox(self):
        with patch.dict(os.environ, {}, clear=True):
            url = "https://api.example.com/v1"
            assert self._resolve(url) == url


# ---------------------------------------------------------------------------
# Helpers for mocking OpenHands SDK structures
# ---------------------------------------------------------------------------


def _make_response_latency(response_id: str, latency: float):
    return SimpleNamespace(response_id=response_id, latency=latency)


def _make_token_usage(prompt_tokens: int, completion_tokens: int = 0):
    return SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


def _make_accumulated(
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def _make_metrics(
    accumulated,
    response_latencies=None,
    token_usages=None,
):
    return SimpleNamespace(
        accumulated_token_usage=accumulated,
        response_latencies=response_latencies or [],
        token_usages=token_usages or [],
    )


def _make_conversation(usage_to_metrics: dict):
    return SimpleNamespace(state=SimpleNamespace(stats=SimpleNamespace(
        usage_to_metrics=usage_to_metrics,
    )))


class TestExtractStats:
    """Tests for OpenHandsAgent._extract_stats."""

    def _extract(self, conversation, model="test-model", tracker=None):
        agent = OpenHandsAgent.__new__(OpenHandsAgent)
        return agent._extract_stats(conversation, model=model, tracker=tracker)

    def test_extract_stats_single_metric(self):
        conv = _make_conversation({
            "anthropic/claude-sonnet": _make_metrics(
                accumulated=_make_accumulated(
                    prompt_tokens=1000, completion_tokens=200,
                    cache_read_tokens=500, cache_write_tokens=100,
                ),
                response_latencies=[_make_response_latency("r1", 2.5)],
                token_usages=[_make_token_usage(1000, 200)],
            ),
        })
        stats = self._extract(conv, model="anthropic/claude-sonnet")
        assert stats.input_tokens == 1000
        assert stats.output_tokens == 200
        assert stats.cache_read_tokens == 500
        assert stats.cache_creation_tokens == 100
        assert stats.num_turns == 1
        assert stats.models == ["anthropic/claude-sonnet"]
        assert stats.llm_ms == 2500
        assert stats.max_input_tokens == 1000

    def test_extract_stats_multi_metric_aggregation(self):
        conv = _make_conversation({
            "model-a": _make_metrics(
                accumulated=_make_accumulated(
                    prompt_tokens=1000, completion_tokens=100,
                    cache_read_tokens=200, cache_write_tokens=50,
                ),
                response_latencies=[
                    _make_response_latency("r1", 1.0),
                    _make_response_latency("r2", 0.5),
                ],
                token_usages=[
                    _make_token_usage(800),
                    _make_token_usage(1000),
                ],
            ),
            "model-b": _make_metrics(
                accumulated=_make_accumulated(
                    prompt_tokens=2000, completion_tokens=300,
                    cache_read_tokens=100, cache_write_tokens=0,
                ),
                response_latencies=[_make_response_latency("r3", 2.0)],
                token_usages=[_make_token_usage(2000)],
            ),
        })
        stats = self._extract(conv, model="")
        assert stats.input_tokens == 3000
        assert stats.output_tokens == 400
        assert stats.cache_read_tokens == 300
        assert stats.cache_creation_tokens == 50
        assert stats.num_turns == 3
        assert stats.llm_ms == 3500
        assert stats.max_input_tokens == 2000
        assert set(stats.models) == {"model-a", "model-b"}

    def test_extract_stats_max_input_tokens(self):
        """max_input_tokens is the max across per-call token_usages, not accumulated."""
        conv = _make_conversation({
            "default": _make_metrics(
                accumulated=_make_accumulated(prompt_tokens=5000),
                response_latencies=[
                    _make_response_latency("r1", 1.0),
                    _make_response_latency("r2", 1.0),
                    _make_response_latency("r3", 1.0),
                ],
                token_usages=[
                    _make_token_usage(1000),
                    _make_token_usage(3000),
                    _make_token_usage(2000),
                ],
            ),
        })
        stats = self._extract(conv, model="my-model")
        assert stats.max_input_tokens == 3000

    def test_extract_stats_default_model_substitution(self):
        """'default' key in usage_to_metrics is replaced with actual model name."""
        conv = _make_conversation({
            "default": _make_metrics(accumulated=_make_accumulated()),
        })
        stats = self._extract(conv, model="anthropic/claude-sonnet-4-5")
        assert stats.models == ["anthropic/claude-sonnet-4-5"]

    def test_extract_stats_no_streaming_no_ttft(self):
        """When tracker is None, ttft_ms=0 and streamed=False."""
        conv = _make_conversation({
            "default": _make_metrics(
                accumulated=_make_accumulated(prompt_tokens=100, completion_tokens=50),
                response_latencies=[_make_response_latency("r1", 1.0)],
                token_usages=[_make_token_usage(100)],
            ),
        })
        stats = self._extract(conv, tracker=None)
        assert stats.ttft_ms == 0
        assert stats.streamed is False

    def test_extract_stats_with_tracker(self):
        """When tracker is present, ttft_ms is derived from chunk timing."""
        tracker = _TTFTTracker()
        # Simulate chunks: first chunk at t=0.1, last chunk at t=0.4
        # for a call with total latency 1.0s
        # decode = 0.5s, ttft = 1.0 - 0.5 = 0.5s = 500ms
        tracker.first_chunk["r1"] = 10.0
        tracker.last_chunk["r1"] = 10.5
        # Second call: decode = 1.0s, ttft = 2.0 - 1.0 = 1.0s = 1000ms
        tracker.first_chunk["r2"] = 20.0
        tracker.last_chunk["r2"] = 21.0

        conv = _make_conversation({
            "default": _make_metrics(
                accumulated=_make_accumulated(prompt_tokens=500, completion_tokens=100),
                response_latencies=[
                    _make_response_latency("r1", 1.0),
                    _make_response_latency("r2", 2.0),
                ],
                token_usages=[_make_token_usage(500)],
            ),
        })
        stats = self._extract(conv, tracker=tracker)
        assert stats.ttft_ms == 1500  # 500 + 1000
        assert stats.streamed is True

    def test_extract_stats_handles_exception_gracefully(self):
        """Bad metrics structure returns empty IterationStats."""
        # conversation.state.stats.usage_to_metrics will raise AttributeError
        conv = SimpleNamespace(state=SimpleNamespace(stats=None))
        stats = self._extract(conv)
        assert stats == IterationStats()

    def test_extract_stats_num_turns_counts_calls(self):
        """num_turns equals total response_latencies count across all metrics."""
        conv = _make_conversation({
            "model-a": _make_metrics(
                accumulated=_make_accumulated(),
                response_latencies=[
                    _make_response_latency("r1", 0.5),
                    _make_response_latency("r2", 0.5),
                ],
            ),
            "model-b": _make_metrics(
                accumulated=_make_accumulated(),
                response_latencies=[
                    _make_response_latency("r3", 1.0),
                ],
            ),
        })
        stats = self._extract(conv)
        assert stats.num_turns == 3
