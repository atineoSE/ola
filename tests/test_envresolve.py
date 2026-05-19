"""Tests for env resolution / fail-fast validation (Option C)."""

import os
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from ola.envresolve import (
    MissingHostVars,
    format_sidecar,
    host_refs,
    missing_host_vars,
    resolved_values,
    validate,
)


def _write(tmp_path, body):
    p = tmp_path / ".env"
    p.write_text(body)
    return p


class TestHostRefs:
    def test_mandatory_vs_optional_vs_internal(self, tmp_path):
        env = _write(
            tmp_path,
            'LLM_API_KEY="${SUBSTRATE_TOKEN}"\n'
            'LLM_BASE_URL="https://${SUBSTRATE_INSTANCE_IP}/v1"\n'
            'OPT="${MAYBE:-fallback}"\n'
            'SELF="base"\n'
            'SELFREF="${SELF}/x"\n',
        )
        mandatory, optional = host_refs(env)
        assert mandatory == {"SUBSTRATE_TOKEN", "SUBSTRATE_INSTANCE_IP"}
        assert optional == {"MAYBE"}
        # SELF is assigned in-file → not host-sourced (python-dotenv's job)

    def test_bare_dollar_is_literal_not_a_ref(self, tmp_path):
        # python-dotenv 1.x does not interpolate bare $VAR → not a ref.
        env = _write(tmp_path, 'PW="a$NOTAREF-b"\n')
        mandatory, optional = host_refs(env)
        assert mandatory == set()
        assert optional == set()

    def test_name_used_both_ways_is_mandatory(self, tmp_path):
        env = _write(tmp_path, 'A="${X}"\nB="${X:-d}"\n')
        mandatory, optional = host_refs(env)
        assert mandatory == {"X"}
        assert optional == set()


class TestValidate:
    def test_missing_lists_sorted(self, tmp_path):
        env = _write(
            tmp_path,
            'A="${ZED}"\nB="${ALPHA}"\nC="${SET_ONE}"\n',
        )
        miss = missing_host_vars(env, {"SET_ONE": "x"})
        assert miss == ["ALPHA", "ZED"]

    def test_empty_value_counts_as_missing(self, tmp_path):
        env = _write(tmp_path, 'A="${HOSTV}"\n')
        assert missing_host_vars(env, {"HOSTV": ""}) == ["HOSTV"]

    def test_validate_raises_with_all_names(self, tmp_path):
        env = _write(tmp_path, 'A="${P}"\nB="${Q}"\n')
        with pytest.raises(MissingHostVars) as ei:
            validate(env, {})
        assert ei.value.names == ["P", "Q"]
        assert "P, Q" in str(ei.value)

    def test_validate_passes_when_present(self, tmp_path):
        env = _write(tmp_path, 'A="${P}"\n')
        validate(env, {"P": "v"})  # no raise

    def test_optional_never_blocks(self, tmp_path):
        env = _write(tmp_path, 'A="${MAYBE:-def}"\n')
        validate(env, {})  # no raise
        assert missing_host_vars(env, {}) == []


class TestResolvedValues:
    def test_interpolates_from_environ_and_validates(self, tmp_path):
        env = _write(
            tmp_path,
            'LLM_BASE_URL="https://${SUBSTRATE_INSTANCE_IP}/v1"\n'
            'LLM_API_KEY="${SUBSTRATE_TOKEN}"\n'
            'OPT="${MAYBE:-fallback}"\n',
        )
        with patch.dict(
            os.environ,
            {"SUBSTRATE_INSTANCE_IP": "10.0.0.5", "SUBSTRATE_TOKEN": "tok"},
        ):
            resolved = resolved_values(env)
        assert resolved["LLM_BASE_URL"] == "https://10.0.0.5/v1"
        assert resolved["LLM_API_KEY"] == "tok"
        assert resolved["OPT"] == "fallback"

    def test_fail_fast_before_resolving(self, tmp_path):
        env = _write(tmp_path, 'LLM_BASE_URL="https://${SUBSTRATE_INSTANCE_IP}/v1"\n')
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(MissingHostVars):
                resolved_values(env)


class TestSidecarRoundTrip:
    def test_roundtrips_special_chars(self, tmp_path):
        src = {
            "A": "https://1.2.3.4/v1",
            "B": 'tok"q\\bs',
            "C": "l1\nl2",
            "D": "",
        }
        sc = tmp_path / "sc.env"
        sc.write_text("\n".join(format_sidecar(src)) + "\n")
        assert dict(dotenv_values(sc)) == src


class TestEnvCommand:
    def _run(self, argv):
        from ola.cli import _env_command

        return _env_command(argv)

    def test_success_prints_sidecar(self, tmp_path, capsys):
        agent = tmp_path / "agent"
        agent.mkdir()
        (agent / ".env").write_text('LLM_BASE_URL="https://${IP}/v1"\n')
        with patch.dict(os.environ, {"IP": "9.9.9.9"}):
            rc = self._run(["-f", str(agent)])
        assert rc == 0
        assert 'LLM_BASE_URL="https://9.9.9.9/v1"' in capsys.readouterr().out

    def test_missing_host_var_exits_1(self, tmp_path, capsys):
        agent = tmp_path / "agent"
        agent.mkdir()
        (agent / ".env").write_text('LLM_BASE_URL="https://${IP}/v1"\n')
        with patch.dict(os.environ, {}, clear=True):
            rc = self._run(["-f", str(agent)])
        assert rc == 1
        assert "Missing required host environment variable(s): IP" in (
            capsys.readouterr().err
        )

    def test_no_env_file_is_ok(self, tmp_path):
        agent = tmp_path / "agent"
        agent.mkdir()
        assert self._run(["-f", str(agent)]) == 0


class TestLoadAgentEnv:
    def test_sandbox_prefers_sidecar(self, tmp_path, monkeypatch):
        from ola import loop

        sidecar = tmp_path / "agent.env"
        sidecar.write_text('LLM_API_KEY="from-sidecar"\n')
        plan = tmp_path / "plan"
        plan.mkdir()
        (plan / ".env").write_text('LLM_API_KEY="${SHOULD_NOT_BE_USED}"\n')

        monkeypatch.setattr("ola.sandbox.SIDECAR_ENV", sidecar)
        monkeypatch.setenv("SANDBOX", "1")
        with patch.dict(os.environ, {}, clear=False):
            loop._load_agent_env(plan)
            assert os.environ["LLM_API_KEY"] == "from-sidecar"

    def test_host_missing_var_fails_fast(self, tmp_path, monkeypatch):
        from ola import loop

        plan = tmp_path / "plan"
        plan.mkdir()
        (plan / ".env").write_text('LLM_BASE_URL="https://${MUST_HAVE}/v1"\n')

        monkeypatch.setattr("ola.sandbox.SIDECAR_ENV", tmp_path / "nope.env")
        monkeypatch.delenv("SANDBOX", raising=False)
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit):
                loop._load_agent_env(plan)

    def test_host_present_loads(self, tmp_path, monkeypatch):
        from ola import loop

        plan = tmp_path / "plan"
        plan.mkdir()
        (plan / ".env").write_text('LLM_BASE_URL="https://${HOSTIP}/v1"\n')

        monkeypatch.setattr("ola.sandbox.SIDECAR_ENV", tmp_path / "nope.env")
        monkeypatch.delenv("SANDBOX", raising=False)
        with patch.dict(os.environ, {"HOSTIP": "7.7.7.7"}):
            loop._load_agent_env(plan)
            assert os.environ["LLM_BASE_URL"] == "https://7.7.7.7/v1"
