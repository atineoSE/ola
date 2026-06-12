"""Tests for ola.sandbox helpers."""

import os
from unittest.mock import patch

from ola.sandbox import sanitize_proxy_env


class TestSanitizeProxyEnv:
    """Strip the bracketed IPv6 entry sbx v0.31.0 adds to NO_PROXY, which
    otherwise makes httpx raise `InvalidURL: Invalid port: ':1]'` on every
    LLM call."""

    def test_drops_bracketed_ipv6_entry(self):
        env = {"NO_PROXY": "localhost,127.0.0.1,::1,[::1],gateway.docker.internal"}
        with patch.dict(os.environ, env, clear=True):
            sanitize_proxy_env()
            assert (
                os.environ["NO_PROXY"]
                == "localhost,127.0.0.1,::1,gateway.docker.internal"
            )

    def test_handles_lowercase_key(self):
        env = {"no_proxy": "::1,[::1],foo"}
        with patch.dict(os.environ, env, clear=True):
            sanitize_proxy_env()
            assert os.environ["no_proxy"] == "::1,foo"

    def test_sanitizes_both_keys(self):
        env = {"NO_PROXY": "[::1],a", "no_proxy": "[::1],b"}
        with patch.dict(os.environ, env, clear=True):
            sanitize_proxy_env()
            assert os.environ["NO_PROXY"] == "a"
            assert os.environ["no_proxy"] == "b"

    def test_leaves_clean_value_untouched(self):
        env = {"NO_PROXY": "localhost,::1,gateway.docker.internal"}
        with patch.dict(os.environ, env, clear=True):
            sanitize_proxy_env()
            assert os.environ["NO_PROXY"] == "localhost,::1,gateway.docker.internal"

    def test_noop_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            sanitize_proxy_env()  # must not raise
            assert "NO_PROXY" not in os.environ
