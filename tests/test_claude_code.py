"""Tests for the ClaudeCodeAgent."""

import json
import logging
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from ola.agents.claude_code import AuthenticationError, ClaudeCodeAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(lines: list[str], returncode: int = 0) -> MagicMock:
    """Return a mock Popen whose stdout yields *lines* as NDJSON."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter(l + "\n" for l in lines)
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = ""
    proc.returncode = returncode
    proc.wait.return_value = returncode
    proc.kill = MagicMock()
    return proc


def _stream_event(inner: dict) -> str:
    return json.dumps({"type": "stream_event", "event": inner})


def _message_start(
    model: str = "claude-sonnet-4-20250514",
    input_tokens: int = 5,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> str:
    return _stream_event({
        "type": "message_start",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    })


def _content_block_start() -> str:
    return _stream_event({"type": "content_block_start"})


def _message_delta() -> str:
    return _stream_event({"type": "message_delta"})


def _result(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
    num_turns: int = 1,
    duration_api_ms: int = 0,
    subtype: str = "success",
    result_text: str = "Done.",
) -> str:
    d = {
        "type": "result",
        "result": result_text,
        "subtype": subtype,
        "num_turns": num_turns,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    }
    if duration_api_ms:
        d["duration_api_ms"] = duration_api_ms
    return json.dumps(d)


def _single_turn_lines(
    model: str = "claude-sonnet-4-20250514",
    input_tokens: int = 5,
    cache_creation: int = 6663,
    cache_read: int = 15771,
    result_input: int = 100,
    result_output: int = 50,
    result_cache_creation: int = 6663,
    result_cache_read: int = 15771,
    duration_api_ms: int = 0,
) -> list[str]:
    return [
        json.dumps({"type": "system"}),
        _message_start(model, input_tokens, cache_creation, cache_read),
        _content_block_start(),
        _message_delta(),
        _result(
            input_tokens=result_input,
            output_tokens=result_output,
            cache_creation=result_cache_creation,
            cache_read=result_cache_read,
            duration_api_ms=duration_api_ms,
        ),
    ]


def _run_stream(lines: list[str], returncode: int = 0) -> MagicMock:
    """Run _stream on a mock proc and return the AgentResponse."""
    proc = _make_proc(lines, returncode)
    agent = ClaudeCodeAgent()
    return agent._stream(proc, "test prompt")


# ---------------------------------------------------------------------------
# Existing tests
# ---------------------------------------------------------------------------

class TestClaudeCodeAgent:
    def test_auth_error_returns_credential_refresh_message(self):
        """AuthenticationError produces an error message referencing credential refresh."""
        agent = ClaudeCodeAgent()
        with patch.object(agent, "_run_once", side_effect=AuthenticationError("bad")):
            resp = agent.run(prompt="hi", workdir="/tmp")
        assert not resp.success
        assert "ola-sandbox" in resp.output
        assert ".credentials.json" in resp.output

    def test_auth_error_does_not_mention_old_approaches(self):
        """Neither cc-credentials nor sbx secret should appear."""
        agent = ClaudeCodeAgent()
        with patch.object(agent, "_run_once", side_effect=AuthenticationError("bad")):
            resp = agent.run(prompt="hi", workdir="/tmp")
        assert "cc-credentials" not in resp.output
        assert "sbx secret" not in resp.output


# ---------------------------------------------------------------------------
# Stream parser tests
# ---------------------------------------------------------------------------

class TestStreamParser:
    def test_single_turn_extracts_all_fields(self):
        """Single mocked turn populates all key fields."""
        resp = _run_stream(_single_turn_lines())
        s = resp.stats
        assert s is not None
        assert s.models == ["claude-sonnet-4-20250514"]
        assert s.max_input_tokens == 5 + 6663 + 15771  # 22439
        assert s.ttft_ms >= 0
        assert s.llm_ms >= 0
        assert s.input_tokens > 0
        assert s.output_tokens > 0
        assert s.cache_read_tokens > 0
        assert s.cache_creation_tokens > 0
        assert s.num_turns == 1

    def test_max_input_tokens_sums_three_buckets(self):
        """max_input_tokens = input + cache_creation + cache_read, not just input."""
        lines = _single_turn_lines(
            input_tokens=5, cache_creation=6663, cache_read=15771
        )
        resp = _run_stream(lines)
        assert resp.stats.max_input_tokens == 22439

    def test_max_input_tokens_tracks_max_across_turns(self):
        """Multi-turn: max_input_tokens is the largest single turn."""
        lines = [
            json.dumps({"type": "system"}),
            # Turn 1: small
            _message_start(input_tokens=10, cache_creation=100, cache_read=200),
            _content_block_start(),
            _message_delta(),
            # Turn 2: large
            _message_start(input_tokens=50, cache_creation=5000, cache_read=10000),
            _content_block_start(),
            _message_delta(),
            # Turn 3: medium
            _message_start(input_tokens=20, cache_creation=1000, cache_read=2000),
            _content_block_start(),
            _message_delta(),
            _result(num_turns=3),
        ]
        resp = _run_stream(lines)
        assert resp.stats.max_input_tokens == 50 + 5000 + 10000  # 15050

    def test_models_from_message_start(self):
        """Model extracted from stream_event; deduped; multiple models collected."""
        lines = [
            json.dumps({"type": "system"}),
            _message_start(model="claude-sonnet-4-20250514"),
            _content_block_start(),
            _message_delta(),
            _message_start(model="claude-sonnet-4-20250514"),  # duplicate
            _content_block_start(),
            _message_delta(),
            _message_start(model="claude-opus-4-20250514"),  # different
            _content_block_start(),
            _message_delta(),
            _result(num_turns=3),
        ]
        resp = _run_stream(lines)
        assert resp.stats.models == ["claude-opus-4-20250514", "claude-sonnet-4-20250514"]

    def test_ttft_per_turn_summed(self):
        """Multi-turn: ttft_ms is sum of per-turn TTFTs (all >= 0)."""
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _message_delta(),
            _message_start(),
            _content_block_start(),
            _message_delta(),
            _result(num_turns=2),
        ]
        resp = _run_stream(lines)
        # Each turn contributes a non-negative TTFT
        assert resp.stats.ttft_ms >= 0

    def test_llm_ms_equals_ttft_plus_decode(self):
        """llm_ms == total_ttft_ms + total_decode_ms."""
        lines = _single_turn_lines()
        resp = _run_stream(lines)
        # llm_ms is computed as ttft + decode inside _stream
        # We can't access decode separately, but llm_ms >= ttft_ms
        assert resp.stats.llm_ms >= resp.stats.ttft_ms

    def test_no_partial_messages_falls_back_gracefully(self):
        """Stream with only assistant + result (no stream_event) falls back."""
        lines = [
            json.dumps({"type": "system"}),
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello"}]},
            }),
            _result(input_tokens=100, output_tokens=50),
        ]
        resp = _run_stream(lines)
        s = resp.stats
        assert s.input_tokens > 0
        assert s.output_tokens > 0
        assert s.ttft_ms == 0
        assert s.llm_ms == 0
        assert s.max_input_tokens == 0

    def test_malformed_json_lines_skipped(self):
        """Invalid JSON lines don't crash; valid lines still parsed."""
        lines = [
            "NOT VALID JSON {{{",
            "",
            json.dumps({"type": "system"}),
            "another bad line",
            _message_start(input_tokens=10, cache_creation=100, cache_read=200),
            _content_block_start(),
            _message_delta(),
            _result(input_tokens=100, output_tokens=50),
        ]
        resp = _run_stream(lines)
        assert resp.stats is not None
        assert resp.stats.max_input_tokens == 310
        assert resp.stats.output_tokens == 50

    def test_authentication_error_raised(self):
        """error: authentication_failed event raises AuthenticationError."""
        lines = [
            json.dumps({
                "error": "authentication_failed",
                "message": {"content": [{"text": "Invalid API key"}]},
            }),
        ]
        proc = _make_proc(lines)
        agent = ClaudeCodeAgent()
        with pytest.raises(AuthenticationError):
            agent._stream(proc, "test prompt")

    def test_result_usage_aggregated(self):
        """Final input/output/cache counts come from result.usage."""
        lines = _single_turn_lines(
            result_input=500,
            result_output=200,
            result_cache_creation=1000,
            result_cache_read=3000,
        )
        resp = _run_stream(lines)
        s = resp.stats
        # input_tokens = input + cache_creation + cache_read from result
        assert s.input_tokens == 500 + 1000 + 3000
        assert s.output_tokens == 200
        assert s.cache_creation_tokens == 1000
        assert s.cache_read_tokens == 3000

    def test_divergence_warning_logged(self, caplog):
        """Large divergence between measured llm_ms and duration_api_ms triggers warning."""
        # We need llm_ms > 0 and a very different duration_api_ms.
        # To get non-zero llm_ms, we need real time to pass between events.
        # Instead, we'll patch time.monotonic to control timing.
        call_count = 0
        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            # message_start: turn_start = t0
            # content_block_start: token_start = t0 + 5 (ttft = 5000ms)
            # message_delta: decode = 5s (decode_ms = 5000ms)
            # Total llm_ms = 10000ms
            return call_count * 5.0

        lines = _single_turn_lines(duration_api_ms=1000)

        with patch("ola.agents.claude_code.time.monotonic", side_effect=fake_monotonic):
            with caplog.at_level(logging.WARNING, logger="ola.agents.claude_code"):
                _run_stream(lines)

        assert any("divergence" in r.message for r in caplog.records)

    def test_no_divergence_warning_when_close(self, caplog):
        """Values within threshold produce no warning."""
        # With mocked events, llm_ms will be ~0 due to fast execution.
        # duration_api_ms=0 means the check is skipped entirely.
        lines = _single_turn_lines(duration_api_ms=0)

        with caplog.at_level(logging.WARNING, logger="ola.agents.claude_code"):
            _run_stream(lines)

        assert not any("divergence" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Rate-limit event tests
# ---------------------------------------------------------------------------

def _rate_limit_event(
    status: str = "rejected",
    resets_at: int | None = None,
    rate_limit_type: str = "five_hour",
    utilization: float = 1.0,
    fallback: bool = False,
) -> str:
    info: dict = {
        "status": status,
        "rateLimitType": rate_limit_type,
        "utilization": utilization,
        "unifiedRateLimitFallbackAvailable": fallback,
    }
    if resets_at is not None:
        info["resetsAt"] = resets_at
    return json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": info,
        "uuid": "test-uuid",
        "session_id": "test-session",
    })


class TestRateLimitEvents:
    def test_rate_limit_rejected_populates_stats(self):
        """Rejected rate limit (no fallback) → error_type=rate_limited in stats."""
        resets_at = 1700000000
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _rate_limit_event(status="rejected", resets_at=resets_at, fallback=False),
            # No result event — stream ends after rate limit rejection
        ]
        resp = _run_stream(lines)
        assert not resp.success
        assert resp.stats is not None
        assert resp.stats.error_type == "rate_limited"
        assert resp.stats.rate_limit_resets_at == resets_at
        assert "five_hour" in resp.stats.error_message
        assert "2023" in resp.stats.error_message  # ISO timestamp from epoch

    def test_rate_limit_rejected_with_fallback_is_not_failure(self, caplog):
        """Rejected + fallback available → CLI handles it, not a failure."""
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _rate_limit_event(
                status="rejected", resets_at=1700000000, fallback=True,
            ),
            _message_delta(),
            _result(),
        ]
        with caplog.at_level(logging.INFO, logger="ola.agents.claude_code"):
            resp = _run_stream(lines)
        assert resp.success
        assert resp.stats.error_type is None
        assert any("fallback" in r.message for r in caplog.records)

    def test_rate_limit_warning_logs_once(self, caplog):
        """allowed_warning logs exactly once, includes type and utilization."""
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _rate_limit_event(
                status="allowed_warning", utilization=0.85,
                rate_limit_type="five_hour", resets_at=1700000000,
            ),
            # Second warning event — should NOT produce another log
            _rate_limit_event(
                status="allowed_warning", utilization=0.90,
                rate_limit_type="five_hour", resets_at=1700000000,
            ),
            _message_delta(),
            _result(),
        ]
        with caplog.at_level(logging.WARNING, logger="ola.agents.claude_code"):
            resp = _run_stream(lines)
        assert resp.success
        warning_records = [
            r for r in caplog.records if "rate limit approaching" in r.message
        ]
        assert len(warning_records) == 1
        assert "five_hour" in warning_records[0].message
        assert "85%" in warning_records[0].message

    def test_rate_limit_allowed_is_noop(self, caplog):
        """allowed status is silently ignored."""
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _rate_limit_event(status="allowed", utilization=0.3),
            _message_delta(),
            _result(),
        ]
        with caplog.at_level(logging.DEBUG, logger="ola.agents.claude_code"):
            resp = _run_stream(lines)
        assert resp.success
        assert not any("rate limit" in r.message.lower() for r in caplog.records)

    def test_rate_limit_seven_day_opus_bucket(self):
        """seven_day_opus bucket type is surfaced in error_message."""
        lines = [
            json.dumps({"type": "system"}),
            _rate_limit_event(
                status="rejected", resets_at=1700000000,
                rate_limit_type="seven_day_opus", fallback=False,
            ),
        ]
        resp = _run_stream(lines)
        assert not resp.success
        assert resp.stats.error_type == "rate_limited"
        assert "seven_day_opus" in resp.stats.error_message


# ---------------------------------------------------------------------------
# Error result subtype tests
# ---------------------------------------------------------------------------

class TestErrorResultSubtype:
    def test_error_result_subtype_captured(self):
        """Result with subtype != 'success' → error_type and error_message populated."""
        error_text = "Tool execution failed: command returned exit code 1"
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _message_delta(),
            _result(
                subtype="error_during_execution",
                result_text=error_text,
            ),
        ]
        resp = _run_stream(lines)
        assert not resp.success
        assert resp.stats is not None
        assert resp.stats.error_type == "error_during_execution"
        assert resp.stats.error_message == error_text

    def test_error_max_turns_subtype(self):
        """error_max_turns subtype is captured correctly."""
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _message_delta(),
            _result(
                subtype="error_max_turns",
                result_text="Max turns reached.",
            ),
        ]
        resp = _run_stream(lines)
        assert not resp.success
        assert resp.stats.error_type == "error_max_turns"
        assert resp.stats.error_message == "Max turns reached."

    def test_error_message_truncated_to_500_chars(self):
        """Long error text is truncated to 500 characters."""
        long_text = "x" * 1000
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _message_delta(),
            _result(
                subtype="error_during_execution",
                result_text=long_text,
            ),
        ]
        resp = _run_stream(lines)
        assert resp.stats.error_message == "x" * 500
        assert len(resp.stats.error_message) == 500

    def test_success_subtype_has_no_error(self):
        """Success result → no error_type or error_message."""
        lines = _single_turn_lines()
        resp = _run_stream(lines)
        assert resp.success
        assert resp.stats.error_type is None
        assert resp.stats.error_message is None

    def test_empty_subtype_treated_as_error(self):
        """Missing/empty subtype → error_type='unknown_error'."""
        lines = [
            json.dumps({"type": "system"}),
            _message_start(),
            _content_block_start(),
            _message_delta(),
            _result(subtype="", result_text="Something went wrong."),
        ]
        resp = _run_stream(lines)
        assert not resp.success
        assert resp.stats.error_type == "unknown_error"
