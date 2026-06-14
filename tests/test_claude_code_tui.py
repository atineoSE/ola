"""Tests for the ClaudeCodeTUIAgent (the ``ct`` PTY-driven backend).

These avoid a real pty: the screen-state predicates and config seeding are pure
and tested directly, and the ``run()`` flow is driven through a scripted fake
``_PtyProcess`` so the end-to-end control flow (ready → paste → turn-end →
teardown) is exercised without spawning ``claude``.
"""

import json
from pathlib import Path

import pytest

from ola.agents import claude_code_tui as ct
from ola.agents.claude_code_tui import (
    ClaudeCodeTUIAgent,
    is_auth_error,
    is_busy,
    is_idle_box,
    is_onboarding,
    is_ready,
    is_trust_dialog,
    seed_claude_json,
)


# ---------------------------------------------------------------------------
# Screen-state predicates
# ---------------------------------------------------------------------------

# Real TUI screens render words with cursor-move escapes, not spaces — so the
# stripped screen is whitespace-free. We test both spaced and spaceless forms.
READY_SPACED = "⏵⏵ bypass permissions on (shift+tab to cycle) · ? for shortcuts"
READY_SPACELESS = "⏵⏵bypasspermissionson(shift+tabtocycle)·?forshortcuts"
TRUST_SCREEN = "Quick safety check: Is this a project you created or one you trust?"
ONBOARDING_SCREEN = "Let's get started.\nChoose the text style that looks best"
BUSY_SCREEN = "✶ Hyperspacing… (6s · esc to interrupt) ⏵⏵ bypass permissions on"
AUTH_SCREEN = "⏺ Please run /login · API Error: 401 Invalid authentication credentials"


@pytest.mark.parametrize("screen", [READY_SPACED, READY_SPACELESS])
def test_is_ready_true(screen):
    assert is_ready(screen)


def test_is_ready_false_on_onboarding_even_with_marker():
    # Onboarding must never be mistaken for ready, even if a marker leaks in.
    assert not is_ready(ONBOARDING_SCREEN + " ? for shortcuts")


def test_is_trust_dialog():
    assert is_trust_dialog(TRUST_SCREEN)
    assert is_trust_dialog("Do you trust the files in this folder?")
    assert not is_trust_dialog(READY_SPACED)


def test_is_onboarding():
    assert is_onboarding(ONBOARDING_SCREEN)
    assert not is_onboarding(READY_SPACED)


def test_is_busy_and_idle_box():
    assert is_busy(BUSY_SCREEN)
    # Busy screen still has the ⏵⏵ marker, but a running turn is NOT an idle box.
    assert not is_idle_box(BUSY_SCREEN)
    assert is_idle_box(READY_SPACED)


def test_is_auth_error():
    assert is_auth_error(AUTH_SCREEN)
    assert not is_auth_error(READY_SPACED)


# ---------------------------------------------------------------------------
# Config seeding (.claude.json)
# ---------------------------------------------------------------------------


def test_seed_claude_json_clones_and_prunes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # A realistic ~/.claude.json with account context and many other projects.
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"accountUuid": "abc"},
                "userID": "u-123",
                "cachedGrowthBookFeatures": {"x": 1},
                "projects": {"/some/other": {}, "/and/another": {}},
            }
        )
    )
    config = tmp_path / "cfg"
    config.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()

    seed_claude_json(config, str(workdir))
    data = json.loads((config / ".claude.json").read_text())

    # Account context preserved; onboarding marked complete.
    assert data["oauthAccount"] == {"accountUuid": "abc"}
    assert data["userID"] == "u-123"
    assert data["cachedGrowthBookFeatures"] == {"x": 1}
    assert data["hasCompletedOnboarding"] is True
    # Projects pruned to just this workdir (realpath), trust pre-accepted.
    assert list(data["projects"]) == [str(Path(workdir).resolve())]
    entry = data["projects"][str(Path(workdir).resolve())]
    assert entry["hasTrustDialogAccepted"] is True


def test_seed_claude_json_minimal_when_no_real_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()  # no ~/.claude.json present
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config = tmp_path / "cfg"
    config.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()

    seed_claude_json(config, str(workdir))
    data = json.loads((config / ".claude.json").read_text())

    assert data["hasCompletedOnboarding"] is True
    assert list(data["projects"]) == [str(Path(workdir).resolve())]


# ---------------------------------------------------------------------------
# Environment construction
# ---------------------------------------------------------------------------


def test_build_env_bootstraps_and_seeds(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    state = tmp_path / "state"
    workdir = tmp_path / "work"
    workdir.mkdir()
    env = ClaudeCodeTUIAgent(model="opus")._build_env(str(workdir), str(state))

    assert env["CLAUDE_CONFIG_DIR"] == str(state)
    assert (state / ".credentials.json").exists()
    assert (state / "settings.json").exists()
    assert (state / ".claude.json").exists()


def test_build_env_self_hosted_skips_credentials(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "my-model")

    state = tmp_path / "state"
    workdir = tmp_path / "work"
    workdir.mkdir()
    env = ClaudeCodeTUIAgent()._build_env(str(workdir), str(state))

    assert env["ANTHROPIC_BASE_URL"].startswith("http")
    # Self-hosted must not bootstrap the Anthropic OAuth credentials.
    assert not (state / ".credentials.json").exists()
    # But onboarding/trust suppression is still seeded.
    assert (state / ".claude.json").exists()


# ---------------------------------------------------------------------------
# run() control flow with a scripted fake pty
# ---------------------------------------------------------------------------


class FakePty:
    """A scripted stand-in for _PtyProcess: ready → (paste) → busy×3 → idle."""

    def __init__(self, ready_screen=READY_SPACELESS, *, never_ready=False):
        self.ready_screen = ready_screen
        self.never_ready = never_ready
        self.paste_done = False
        self._busy_polls = 0
        self._alive = True
        self.sent: list[bytes] = []

    def tail(self, n=4000):
        if not self.paste_done:
            return "" if self.never_ready else self.ready_screen
        return BUSY_SCREEN if self._busy_polls < 3 else self.ready_screen

    def idle_for(self):
        if not self.paste_done:
            return 99.0
        if self._busy_polls < 3:
            self._busy_polls += 1
            return 0.1  # active → marks saw_activity
        return 99.0  # quiescent → turn end

    def send(self, data: bytes):
        self.sent.append(data)
        if b"\x1b[201~" in data:  # the bracketed-paste terminator
            self.paste_done = True

    def alive(self):
        return self._alive

    def close(self):
        self._alive = False


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ct.time, "sleep", lambda *_: None)


def test_run_success_flow(monkeypatch):
    fake = FakePty()
    monkeypatch.setattr(ct, "_PtyProcess", lambda *a, **k: fake)
    progress: list[str] = []

    resp = ClaudeCodeTUIAgent(model="opus").run(
        "do the task",
        "/tmp/wd",
        state_dir=None,
        on_progress=lambda msg, metrics=None: progress.append(msg),
    )

    assert resp.success is True
    assert resp.stats.streamed is False  # no metrics recovered by design
    assert resp.stats.models == ["opus"]
    # The prompt was bracket-pasted and submitted.
    assert any(b"\x1b[200~" in s for s in fake.sent)
    assert b"\r" in fake.sent
    # Teardown asked the TUI to exit and the process was closed.
    assert any(b"/exit" in s for s in fake.sent)
    assert fake.alive() is False
    assert progress  # at least one progress ping emitted


def test_run_not_ready_fails(monkeypatch):
    fake = FakePty(never_ready=True)
    monkeypatch.setattr(ct, "_PtyProcess", lambda *a, **k: fake)
    # Make the ready wait expire immediately.
    monkeypatch.setattr(ct, "_ready_timeout", lambda: 0.0)

    resp = ClaudeCodeTUIAgent().run("x", "/tmp/wd", state_dir=None)
    assert resp.success is False
    assert resp.stats.error_type == "tui_not_ready"


def test_run_auth_error_returns_friendly_message(monkeypatch):
    class AuthPty(FakePty):
        def tail(self, n=4000):
            return AUTH_SCREEN

    fake = AuthPty()
    monkeypatch.setattr(ct, "_PtyProcess", lambda *a, **k: fake)

    resp = ClaudeCodeTUIAgent().run("x", "/tmp/wd", state_dir=None)
    assert resp.success is False
    assert resp.stats.error_type == "authentication_error"
    assert "Authentication failed" in resp.output


def test_run_pty_alloc_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("out of pty devices")

    monkeypatch.setattr(ct, "_PtyProcess", boom)
    resp = ClaudeCodeTUIAgent().run("x", "/tmp/wd", state_dir=None)
    assert resp.success is False
    assert resp.stats.error_type == "pty_alloc_failed"


def test_run_cli_not_found(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(ct, "_PtyProcess", missing)
    resp = ClaudeCodeTUIAgent().run("x", "/tmp/wd", state_dir=None)
    assert resp.success is False
    assert resp.stats.error_type == "cli_not_found"
