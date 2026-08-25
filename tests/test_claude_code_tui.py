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
    _transcript_paths,
    is_auth_error,
    is_rate_limited,
    is_busy,
    is_idle_box,
    is_onboarding,
    is_ready,
    is_trust_dialog,
    seed_claude_json,
    transcript_stats,
)


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    """Default every test to "no macOS Keychain" (i.e. the sandbox).

    ``_build_env`` clears the entry shadowing the per-task .credentials.json,
    which shells out to `security`. Leaving the real binary discoverable would
    have the suite mutate the developer's own Keychain. The test that actually
    exercises the cleanup re-patches shutil.which itself.
    """
    monkeypatch.setattr("ola.agents.claude_code.shutil.which", lambda _name: None)


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
LIMIT_SCREEN = (
    "Claude usage limit reached. Your limit will reset at 3pm (Europe/Madrid)."
)
LIMIT_SCREEN_SHORT = "5-hour limit reached ∙ resets 3pm"
LIMIT_WARNING_SCREEN = "Approaching usage limit · /model to use best available model"


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
    # Regression: the "/" in "/login" must not defeat detection, and the
    # "Not logged in" footer (shown before a turn even starts) must be caught.
    assert is_auth_error("⎿ Not logged in · Please run /login")
    assert is_auth_error("← for agents Not logged in · Run /login")
    assert not is_auth_error(READY_SPACED)


def test_is_rate_limited():
    assert is_rate_limited(LIMIT_SCREEN)
    assert is_rate_limited(LIMIT_SCREEN_SHORT)
    assert is_rate_limited("You've reached your weekly limit for Claude Opus")
    # The TUI renders words without spaces (cursor-move escapes).
    assert is_rate_limited("Claudeusagelimitreached.Yourlimitwillresetat3pm")
    # A *warning* is not a stop — the CLI keeps running on the fallback model,
    # so treating it as global would abort a run that is still working.
    assert not is_rate_limited(LIMIT_WARNING_SCREEN)
    assert not is_rate_limited(READY_SPACED)
    assert not is_rate_limited(BUSY_SCREEN)
    # The two global-stop detectors must not claim each other's screens.
    assert not is_rate_limited(AUTH_SCREEN)
    assert not is_auth_error(LIMIT_SCREEN)


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


def _keychain_calls(monkeypatch, tmp_path, *, self_hosted=False, security="/usr/bin/security"):
    """Build a per-task config dir via _build_env; return `security` argv lists."""
    from ola.agents import claude_code as cc

    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_SKIP_TLS_VERIFY"):
        monkeypatch.delenv(k, raising=False)
    if self_hosted:
        monkeypatch.setenv("LLM_BASE_URL", "https://my-host.example.com")

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text('{"token": "x"}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    monkeypatch.setattr(cc.shutil, "which", lambda _name: security)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cc.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or _completed(cmd),
    )

    workdir = tmp_path / "work"
    workdir.mkdir()
    state_dir = tmp_path / "phase" / ".claude" / "t-abc1234"
    state_dir.mkdir(parents=True)
    ClaudeCodeTUIAgent()._build_env(str(workdir), str(state_dir))
    return calls, state_dir


def _completed(cmd):
    import subprocess

    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_build_env_deletes_shadowing_keychain_entry(monkeypatch, tmp_path):
    """``ct`` uses the same per-task config dirs as ``cc`` — same macOS trap.

    The Keychain entry keyed to the config dir outranks the .credentials.json
    just copied into it, and the dir is derived from the task id, so it is
    stable across runs: one token that expires mid-flight poisons that task id
    permanently, and cc-credentials cannot fix it because the file is never
    read.
    """
    from ola.agents.claude_code import _keychain_service

    calls, state_dir = _keychain_calls(monkeypatch, tmp_path)

    assert [
        "/usr/bin/security",
        "delete-generic-password",
        "-s",
        _keychain_service(str(state_dir)),
    ] in calls


def test_build_env_self_hosted_skips_keychain_delete(monkeypatch, tmp_path):
    """Self-hosted uses ANTHROPIC_AUTH_TOKEN — no OAuth entry to clear."""
    calls, _ = _keychain_calls(monkeypatch, tmp_path, self_hosted=True)

    assert not [c for c in calls if "delete-generic-password" in c]


def test_build_env_keychain_delete_noop_without_security(monkeypatch, tmp_path):
    """In the Linux sandbox there is no `security` — the file already wins."""
    calls, _ = _keychain_calls(monkeypatch, tmp_path, security=None)

    assert not [c for c in calls if "delete-generic-password" in c]


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
    monkeypatch.setattr(ct, "_READY_TIMEOUT_SEC", 0.0)

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


def test_run_rate_limited_escalates_instead_of_looking_successful(monkeypatch):
    """A limited turn goes silent at once — quiescence must not read as done.

    Regression for the stall the ``cc`` backend hit: the scheduler saw an agent
    that "succeeded" without ticking its checkbox, called it stagnant, and let
    every task burn its attempts against a wall that had not moved. Here the
    banner is on screen *and* the pty has gone quiet after printing it, which is
    exactly the shape the end-of-turn heuristic would otherwise accept.
    """

    class LimitedPty(FakePty):
        """Banner is printed (activity), then the pty goes quiet for good."""

        def tail(self, n=4000):
            return self.ready_screen if not self.paste_done else LIMIT_SCREEN

        def idle_for(self):
            if not self.paste_done:
                return 99.0
            self._busy_polls += 1
            # One poll of activity — the banner reaching the pty — then silence,
            # which is precisely what _await_turn_end accepts as end-of-turn.
            return 0.1 if self._busy_polls == 1 else 99.0

    fake = LimitedPty()
    monkeypatch.setattr(ct, "_PtyProcess", lambda *a, **k: fake)

    resp = ClaudeCodeTUIAgent(model="opus").run("x", "/tmp/wd", state_dir=None)

    assert resp.success is False
    # What the scheduler keys on to abort the run once (exit 41 + marker)
    # rather than fail task-by-task.
    assert resp.stats.error_type == "rate_limited"
    # The TUI shows the reset time as prose, so there is no epoch for the
    # marker — ola-monitor falls back to its floor wait.
    assert resp.stats.rate_limit_resets_at is None
    assert resp.stats.error_message == ct._RATE_LIMIT_MESSAGE
    # The banner itself stays diagnosable in the output.
    assert "usage limit reached" in resp.output
    # Teardown still ran, so no pty is leaked on the escalation path.
    assert fake.alive() is False


def test_run_rate_limited_before_the_prompt_is_pasted(monkeypatch):
    """A banner already on screen at startup must not wait out _READY_TIMEOUT."""

    class LimitedPty(FakePty):
        def tail(self, n=4000):
            return LIMIT_SCREEN

    monkeypatch.setattr(ct, "_PtyProcess", lambda *a, **k: LimitedPty())

    resp = ClaudeCodeTUIAgent().run("x", "/tmp/wd", state_dir=None)
    assert resp.success is False
    assert resp.stats.error_type == "rate_limited"


def test_run_logs_the_screen_tail_when_no_banner_matched(monkeypatch, caplog):
    """A missed limit banner must not vanish.

    _LIMIT_MARKERS are not yet pinned to a live capture, so a banner they miss
    reads as a finished-but-unticked turn — and the scheduler drops `output` on
    that path. The DEBUG tail is the only surviving evidence, and reproducing it
    otherwise costs another five-hour window.
    """
    import logging

    fake = FakePty()
    monkeypatch.setattr(ct, "_PtyProcess", lambda *a, **k: fake)

    with caplog.at_level(logging.DEBUG, logger="ola.agents.claude_code_tui"):
        ClaudeCodeTUIAgent().run("x", "/tmp/wd", state_dir=None)

    tails = [r for r in caplog.records if "end-of-turn screen tail" in r.message]
    assert tails, "no screen tail logged"
    assert READY_SPACELESS in tails[-1].getMessage()


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


# ---------------------------------------------------------------------------
# Transcript metric recovery (.claude folder, post-turn)
# ---------------------------------------------------------------------------


def _assistant_line(model="claude-opus-4-8", inp=0, out=0, cache_c=0, cache_r=0):
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "model": model,
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_creation_input_tokens": cache_c,
                    "cache_read_input_tokens": cache_r,
                },
            },
        }
    )


_TWO_TURN = "\n".join(
    [
        json.dumps({"type": "user", "message": {"role": "user"}}),
        _assistant_line(inp=100, out=20, cache_c=10, cache_r=5000),
        # A synthetic record with no usage must not count as a turn or a model.
        json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}),
        _assistant_line(inp=50, out=30, cache_c=0, cache_r=8000),
    ]
)


def test_transcript_stats_sums_usage_and_skips_synthetic():
    s = transcript_stats(_TWO_TURN)
    assert s.num_turns == 2  # synthetic record excluded
    assert s.output_tokens == 50  # 20 + 30
    assert s.cache_read_tokens == 13000  # 5000 + 8000
    assert s.cache_creation_tokens == 10
    assert s.input_tokens == 100 + 50 + 10 + 13000  # total prompt incl. cache
    assert s.max_input_tokens == 50 + 8000  # peak per-turn context (turn 2)
    assert s.models == ["claude-opus-4-8"]  # "<synthetic>" not collected
    assert s.streamed is False


def test_transcript_stats_reconstructs_tool_time():
    """Tool wall-time = the gap between an assistant tool_use and its result."""
    text = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-06-14T08:00:00.000Z",
                    "message": {
                        "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "content": [{"type": "tool_use", "name": "Bash"}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-06-14T08:00:02.500Z",  # +2.5s tool run
                    "message": {"content": [{"type": "tool_result"}]},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-06-14T08:00:10.000Z",
                    "message": {
                        "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 12, "output_tokens": 8},
                        "content": [{"type": "text", "text": "done"}],
                    },
                }
            ),
        ]
    )
    s = transcript_stats(text)
    assert s.tool_ms == 2500  # 08:00:02.5 − 08:00:00
    assert s.num_turns == 2  # both assistant messages had usage
    # span 08:00:00 → 08:00:10 = 10s; decode = span − tool = 7.5s → drives a
    # non-zero tok/sec so the dashboard's peak tile is populated.
    assert s.decode_ms == 7500
    assert s.llm_ms == 7500


def test_transcript_stats_no_tool_time_without_tools():
    s = transcript_stats(_TWO_TURN)
    assert s.tool_ms == 0  # no timestamps/tool_use
    assert s.decode_ms == 0  # no timestamps → no span


def test_transcript_stats_empty_on_failed_session():
    # An aborted attempt's transcript carries only synthetic / no-usage records.
    text = json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}})
    s = transcript_stats(text)
    assert s.num_turns == 0
    assert s.output_tokens == 0


def _write_transcript(cfg: Path, slug: str, name: str, text: str) -> Path:
    proj = cfg / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{name}.jsonl"
    path.write_text(text)
    return path


def test_recover_stats_reads_new_transcript(tmp_path):
    cfg = tmp_path / "cfg"
    # A prior attempt's transcript that must be ignored.
    _write_transcript(cfg, "slug", "old-session", _assistant_line(inp=1, out=1))
    before = _transcript_paths(cfg)
    # This attempt's transcript appears during the run.
    _write_transcript(cfg, "slug", "new-session", _TWO_TURN)

    stats = ClaudeCodeTUIAgent(model="opus")._recover_stats(cfg, before, None)
    assert stats.num_turns == 2  # the new transcript, not the old one
    assert stats.output_tokens == 50
    assert stats.error_type is None


def test_recover_stats_stamps_error_type(tmp_path):
    cfg = tmp_path / "cfg"
    before = _transcript_paths(cfg)
    _write_transcript(cfg, "slug", "s", _TWO_TURN)
    stats = ClaudeCodeTUIAgent()._recover_stats(cfg, before, "turn_timeout")
    assert stats.num_turns == 2
    assert stats.error_type == "turn_timeout"


def test_recover_stats_fallback_when_no_transcript(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "projects").mkdir(parents=True)
    before = _transcript_paths(cfg)  # empty
    stats = ClaudeCodeTUIAgent(model="opus")._recover_stats(cfg, before, None)
    assert stats.num_turns == 0
    assert stats.models == ["opus"]  # minimal fallback
    assert stats.streamed is False


def test_recover_stats_no_state_dir(tmp_path):
    stats = ClaudeCodeTUIAgent(model="opus")._recover_stats(None, set(), None)
    assert stats.num_turns == 0
    assert stats.models == ["opus"]
