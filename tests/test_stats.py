"""Tests for the shared stats helpers."""

from ola.stats import cache_hit_rate


def test_cache_hit_rate_typical():
    # 80 of 100 total prompt tokens were cache reads → 80%
    assert cache_hit_rate(100, 80) == 80.0


def test_cache_hit_rate_none_cached():
    assert cache_hit_rate(500, 0) == 0.0


def test_cache_hit_rate_all_cached():
    assert cache_hit_rate(1000, 1000) == 100.0


def test_cache_hit_rate_zero_input():
    assert cache_hit_rate(0, 0) == 0.0


def test_cache_hit_rate_realistic_openhands():
    # Based on real OpenHands trajectory:
    # prompt_tokens=4_371_273  cache_read_tokens=4_266_464
    rate = cache_hit_rate(4_371_273, 4_266_464)
    assert abs(rate - 97.6) < 0.1
