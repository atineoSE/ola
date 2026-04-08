"""Tests for openhands agent helpers and sandbox utilities."""

import os
from unittest.mock import patch

import pytest

from ola.sandbox import is_sandbox


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
