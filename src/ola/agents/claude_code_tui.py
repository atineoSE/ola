"""Claude Code backend that drives the *interactive TUI* through a PTY.

This is an alternative to the headless ``cc`` backend (``claude -p``). Instead of
the one-shot ``--print`` stream, it spawns the full interactive ``claude`` UI in a
pseudo-terminal, pastes the task prompt, waits for the turn to finish, and tears
the session down.

Why it is shaped the way it is — every point below was verified against the real
CLI during a spike (see git history / the ``ct`` skill):

* **No GUI.** A ``pty.openpty()`` master/slave pair is enough; the TUI only needs
  *a* tty, not a window. (The host command-sandbox may deny pty allocation —
  "out of pty devices"; ola runs agents inside the Docker sandbox where it is
  allowed.)
* **First-run gates.** A fresh ``CLAUDE_CONFIG_DIR`` triggers onboarding (theme
  picker) and the workspace-trust dialog — neither of which the ``-p`` path ever
  renders. We pre-seed ``.claude.json`` (``hasCompletedOnboarding`` + a
  per-project ``hasTrustDialogAccepted`` keyed by the **realpath** of the
  workdir) to skip both, and keep an Enter-to-accept fallback for the trust
  dialog in case the seed misses.
* **Completion signal.** The interactive TUI does not stream usage to us, so we
  detect end-of-turn from the **screen** going idle (not from a result event).
  That is acceptable because ola's only real completion signal is the ticked
  PLAN.md checkbox (checkbox-is-truth); the harness re-derives success from the
  worktree regardless of what this backend returns.
* **Metrics, post-turn.** Any session that runs long enough to flush persists a
  full transcript under ``<CLAUDE_CONFIG_DIR>/projects/<slug>/<session>.jsonl``.
  On teardown (after ``/exit`` flushes it) we read that transcript and recover
  per-task token counts, turn count, models, and peak context — enough for cost
  and cache-hit reporting (see :func:`transcript_stats`). What is **not**
  recoverable is the streaming-only timing: TTFT and decode-isolated tok/sec are
  never written to disk. A session too short to flush falls back to minimal
  stats. So ``ct`` reports token economics but, unlike ``cc``, no live timing.
"""

import fcntl
import json
import logging
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from datetime import datetime
from pathlib import Path

from ola.agents.base import AgentResponse, ProgressCallback
from ola.agents.claude_code import (
    AuthenticationError,
    ClaudeCodeAgent,
    _is_self_hosted,
    _self_hosted_env_overlay,
)
from ola.stats import IterationStats

logger = logging.getLogger(__name__)

# A pty needs a window size or the full-screen TUI will not render its box.
_PTY_ROWS, _PTY_COLS = 50, 120

# Files copied verbatim from ~/.claude into the per-task CONFIG_DIR. ``.claude.json``
# is intentionally NOT here — it lives at ~/.claude.json (home root), and we build
# a pruned one ourselves in ``seed_claude_json``.
_BOOTSTRAP_FILES = (".credentials.json", "settings.json")
_ALWAYS_REFRESH = {".credentials.json"}


# Tunables (seconds). Turn timeout is generous — interactive tasks can be long.
_READY_TIMEOUT_SEC = 90.0
_TURN_TIMEOUT_SEC = 3600.0
_READY_QUIESCENCE_SEC = 1.5
# Conservative: the spinner animates while the model thinks or a tool runs, so
# the pty only goes silent this long when the turn is genuinely over.
_DONE_QUIESCENCE_SEC = 5.0


_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
# OSC sequences (e.g. terminal-title sets: ESC ] 0 ; text BEL / ST).
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    """Drop CSI/OSC escape sequences and carriage returns from raw pty bytes."""
    return _OSC_RE.sub("", _CSI_RE.sub("", text)).replace("\r", "")


def compact(text: str) -> str:
    """Whitespace-stripped, lowercased screen text.

    The TUI positions words with cursor-move escapes rather than literal spaces,
    so a stripped screen reads like ``Quicksafetycheck:Isthis…``. Matching on
    multi-word phrases fails unless whitespace is removed first.
    """
    return re.sub(r"\s+", "", strip_ansi(text)).lower()


# --- Screen-state predicates (pure; unit-tested without a pty) ---

# Idle input-box markers — the footer/box shown when claude is waiting for input.
_READY_MARKERS = ("?forshortcuts", "forshortcuts", "⏵⏵", "pasteagain")
# Substrings of the workspace-trust dialog.
_TRUST_MARKERS = ("doyoutrust", "trustthefiles", "trustthisfolder", "quicksafetycheck")
# First-run onboarding (theme picker etc.).
_ONBOARDING_MARKERS = ("choosethetextstyle", "let'sgetstarted")
# Shown in the footer while a turn is actively running.
_BUSY_MARKERS = ("esctointerrupt",)
# Authentication failure banners. NB: keep these robust to the "/" in "/login"
# (compacted text reads "pleaserun/login", "notloggedin·run/login").
_AUTH_MARKERS = (
    "notloggedin",
    "pleaserun/login",
    "invalidauthenticationcredentials",
    "/login·apierror",
)


def is_ready(screen: str) -> bool:
    c = compact(screen)
    return (not is_onboarding(screen)) and any(m in c for m in _READY_MARKERS)


def is_trust_dialog(screen: str) -> bool:
    return any(m in compact(screen) for m in _TRUST_MARKERS)


def is_onboarding(screen: str) -> bool:
    return any(m in compact(screen) for m in _ONBOARDING_MARKERS)


def is_busy(screen: str) -> bool:
    return any(m in compact(screen) for m in _BUSY_MARKERS)


def is_auth_error(screen: str) -> bool:
    return any(m in compact(screen) for m in _AUTH_MARKERS)


def is_idle_box(screen: str) -> bool:
    """True when the idle input box is present and no turn is running."""
    return is_ready(screen) and not is_busy(screen)


def seed_claude_json(config_dir: Path, workdir: str) -> None:
    """Write ``<config_dir>/.claude.json`` so the TUI skips first-run gates.

    Clones the real ``~/.claude.json`` when present (to preserve the
    ``oauthAccount``/``userID`` context interactive auth needs on hosts that
    don't use a file-based credential store), but PRUNES ``projects`` down to
    just this workdir so we don't leak the user's other project paths. When the
    real file is absent (e.g. a fresh Docker container) a minimal seed is enough,
    because there credentials come from the bootstrapped ``.credentials.json``.
    """
    real = Path.home() / ".claude.json"
    data: dict = {}
    if real.exists():
        try:
            data = json.loads(real.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read %s; using minimal .claude.json seed", real)
            data = {}
    data["hasCompletedOnboarding"] = True
    # claude canonicalises cwd (macOS /var -> /private/var); the project key MUST
    # be the realpath or the trust lookup misses and the dialog reappears.
    data["projects"] = {
        os.path.realpath(workdir): {
            "hasTrustDialogAccepted": True,
            "allowedTools": [],
            "projectOnboardingSeenCount": 1,
        }
    }
    (config_dir / ".claude.json").write_text(json.dumps(data))


def _ts(value: object) -> datetime | None:
    """Parse a transcript record's ISO-8601 ``timestamp`` (trailing 'Z')."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_block(content: object, kind: str) -> bool:
    """True when a message ``content`` list holds a block of type ``kind``."""
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == kind for b in content
    )


def transcript_stats(text: str) -> IterationStats:
    """Recover token/turn/model/timing metrics from a Claude Code transcript JSONL.

    The interactive TUI does not stream usage to us, but for any session that
    runs long enough to flush it persists a full transcript under
    ``<CLAUDE_CONFIG_DIR>/projects/<slug>/<session>.jsonl``. Each ``assistant``
    record carries a ``message.usage`` block, so summing across turns recovers
    the token economics; tool wall-time is reconstructed from record timestamps
    — the gap between an assistant ``tool_use`` and its following ``tool_result``
    is how long that tool ran. The one thing that is never written to disk is the
    streaming-only timing (TTFT and decode-isolated tok/sec), so those stay 0 and
    ``streamed`` is False, matching the honest "no live timing" contract.
    """
    input_t = output_t = cache_r = cache_c = 0
    turns = 0
    max_ctx = 0
    models: list[str] = []
    tool_s = 0.0
    pending_tool_ts: datetime | None = None
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        rtype = obj.get("type")
        msg = obj.get("message") or {}
        content = msg.get("content")
        ts = _ts(obj.get("timestamp"))
        if ts is not None:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)

        if rtype == "assistant":
            usage = msg.get("usage") or {}
            it = usage.get("input_tokens", 0)
            cc = usage.get("cache_creation_input_tokens", 0)
            cr = usage.get("cache_read_input_tokens", 0)
            ot = usage.get("output_tokens", 0)
            # Count only real LLM turns; skip synthetic records (compaction
            # summaries etc.) that carry no usage and a "<synthetic>" model.
            if it or cc or cr or ot:
                turns += 1
                input_t += it
                cache_c += cc
                cache_r += cr
                output_t += ot
                max_ctx = max(max_ctx, it + cc + cr)
                model = msg.get("model")
                if model and model not in models:
                    models.append(model)
            # A tool_use opens a tool interval that the next tool_result closes.
            if ts is not None and _has_block(content, "tool_use"):
                pending_tool_ts = ts
        elif rtype == "user" and pending_tool_ts is not None and ts is not None:
            if _has_block(content, "tool_result") or "toolUseResult" in obj:
                tool_s += (ts - pending_tool_ts).total_seconds()
                pending_tool_ts = None

    tool_ms = max(0, int(tool_s * 1000))
    # Approximate "decode" (LLM) time as the transcript span minus tool wall
    # time. We have no true streaming decode clock, but this drives a tok/sec
    # (output / decode) that excludes tool time — so the dashboard's per-task
    # rate, and thus its peak/max tile, are non-zero. ttft is unknown (0), so
    # llm_ms == decode_ms.
    span_ms = (
        int((last_ts - first_ts).total_seconds() * 1000)
        if first_ts is not None and last_ts is not None
        else 0
    )
    decode_ms = max(0, span_ms - tool_ms)
    return IterationStats(
        # input_tokens is the total prompt size (incl. cache), matching the cc
        # adapter so cache_hit_rate and totals read the same across backends.
        input_tokens=input_t + cache_c + cache_r,
        output_tokens=output_t,
        cache_read_tokens=cache_r,
        cache_creation_tokens=cache_c,
        num_turns=turns,
        models=models,
        max_input_tokens=max_ctx,
        # Wall time spent in tools (assistant tool_use → tool_result gaps). With
        # wall_ms from the scheduler this yields the LLM/Tool breakdown.
        tool_ms=tool_ms,
        decode_ms=decode_ms,
        llm_ms=decode_ms,
        streamed=False,
    )


def _transcript_paths(config_dir: Path) -> set[Path]:
    """Main-session transcript files under a config dir's ``projects/`` tree.

    Matches ``projects/<slug>/<session>.jsonl`` only — the deeper
    ``…/<session>/subagents/*.jsonl`` are excluded so a sub-agent's transcript
    never masquerades as the task's own session.
    """
    return set((config_dir / "projects").glob("*/*.jsonl"))


class _PtyProcess:
    """A child process attached to a pseudo-terminal, with a background reader.

    The reader thread accumulates raw pty output so callers can poll a decoded
    tail and measure how long the screen has been quiescent (the end-of-turn
    signal). All OS/pty specifics live here so the rest of the backend stays
    testable.
    """

    def __init__(self, argv: list[str], cwd: str, env: dict[str, str]):
        import pty  # local import: posix-only

        self.master, slave = pty.openpty()
        fcntl.ioctl(
            self.master,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0),
        )
        self.proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,  # own process group, so we can kill the tree
            close_fds=True,
        )
        os.close(slave)
        self._raw = bytearray()
        self._lock = threading.Lock()
        self._last_read = time.monotonic()
        self._stop = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._stop:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break  # EIO on child exit (Linux)
            if not data:
                break  # EOF (macOS)
            with self._lock:
                self._raw.extend(data)
                self._last_read = time.monotonic()

    def tail(self, n: int = 4000) -> str:
        with self._lock:
            return bytes(self._raw[-n:]).decode("utf-8", "replace")

    def idle_for(self) -> float:
        """Seconds since the last byte arrived from the child."""
        with self._lock:
            return time.monotonic() - self._last_read

    def send(self, data: bytes) -> None:
        try:
            os.write(self.master, data)
        except OSError:
            logger.debug("write to pty failed (child likely gone)")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        """Stop the reader and make sure the whole process group is gone."""
        self._stop = True
        if self.alive():
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            os.close(self.master)
        except OSError:
            pass


def _paste(child: _PtyProcess, prompt: str) -> None:
    """Inject a (multiline) prompt via bracketed paste, then submit with Enter.

    Bracketed paste makes the TUI treat embedded newlines as inserted text
    rather than as submit, so the whole prompt lands in one input before Enter.
    """
    child.send(b"\x1b[200~" + prompt.encode("utf-8") + b"\x1b[201~")
    time.sleep(0.5)
    child.send(b"\r")


class ClaudeCodeTUIAgent(ClaudeCodeAgent):
    """Drive the interactive Claude Code TUI in a pseudo-terminal.

    Inherits ``version()`` and ``state_dir_name`` from :class:`ClaudeCodeAgent`
    but replaces the headless ``-p`` invocation with a full PTY-driven session.
    """

    mnemonic = "ct"
    full_name = "Claude Code (TUI)"

    def run(
        self,
        prompt: str,
        workdir: str,
        state_dir: str | None = None,
        labels: dict[str, str] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AgentResponse:
        try:
            return self._run_tui(prompt, workdir, state_dir, on_progress)
        except AuthenticationError:
            return AgentResponse(
                output=(
                    "Authentication failed. Run `ola-sandbox <name>` to refresh "
                    "credentials (copies ~/.claude/.credentials.json into sandbox)."
                ),
                success=False,
                stats=IterationStats(
                    streamed=False,
                    error_type="authentication_error",
                    models=[self.model] if self.model else [],
                ),
            )

    # --- internals ---

    def _build_env(self, workdir: str, state_dir: str | None) -> dict[str, str]:
        self_hosted = _is_self_hosted()
        env = {**os.environ}
        if state_dir:
            sd = Path(state_dir)
            sd.mkdir(parents=True, exist_ok=True)
            if not self_hosted:
                home_claude = Path.home() / ".claude"
                for fname in _BOOTSTRAP_FILES:
                    src = home_claude / fname
                    dst = sd / fname
                    if src.exists() and (fname in _ALWAYS_REFRESH or not dst.exists()):
                        shutil.copy2(src, dst)
            seed_claude_json(sd, workdir)
            env["CLAUDE_CONFIG_DIR"] = str(sd)
        if self_hosted:
            env.update(_self_hosted_env_overlay(self.model))
        return env

    def _stats(self, error_type: str | None = None) -> IterationStats:
        # The interactive TUI exposes no machine-readable usage to us, so stats
        # are deliberately minimal. ``streamed=False`` flags that token/timing
        # fields are absent (not zero-because-idle).
        return IterationStats(
            streamed=False,
            models=[self.model] if self.model else [],
            error_type=error_type,
        )

    def _run_tui(
        self,
        prompt: str,
        workdir: str,
        state_dir: str | None,
        on_progress: ProgressCallback | None,
    ) -> AgentResponse:
        env = self._build_env(workdir, state_dir)
        argv = ["claude", "--dangerously-skip-permissions"]
        if self.model:
            argv += ["--model", self.model]
        logger.debug("ct: spawning interactive TUI: %s", " ".join(argv))

        # Snapshot pre-existing transcripts so we can pick out *this* attempt's
        # after teardown (the per-task config dir accumulates one per attempt).
        config_dir = Path(state_dir) if state_dir else None
        before = _transcript_paths(config_dir) if config_dir else set()

        try:
            child = _PtyProcess(argv, workdir, env)
        except FileNotFoundError:
            return AgentResponse(
                output="'claude' CLI not found. Install it first.",
                success=False,
                stats=self._stats(error_type="cli_not_found"),
            )
        except OSError as exc:
            # e.g. "out of pty devices" when the sandbox forbids pty allocation.
            return AgentResponse(
                output=f"could not allocate a pty for the TUI: {exc}",
                success=False,
                stats=self._stats(error_type="pty_alloc_failed"),
            )

        output = ""
        success = False
        error_type: str | None = None
        try:
            if not self._await_ready(child):
                output = "TUI never reached a ready prompt:\n" + strip_ansi(
                    child.tail(2000)
                )
                error_type = "tui_not_ready"
            else:
                _paste(child, prompt)
                if on_progress is not None:
                    try:
                        on_progress("running (interactive TUI)…", None)
                    except Exception:
                        logger.exception("on_progress raised; continuing")
                done = self._await_turn_end(child, on_progress)
                output = strip_ansi(child.tail(2000))
                if done:
                    success = True
                else:
                    output = "timed out waiting for the turn to finish:\n" + output
                    error_type = "turn_timeout"
        finally:
            # Teardown sends /exit, which is what makes the TUI flush its
            # transcript — so stats are recovered *after* this returns.
            self._teardown(child)

        stats = self._recover_stats(config_dir, before, error_type)
        return AgentResponse(output=output, success=success, stats=stats)

    def _recover_stats(
        self,
        config_dir: Path | None,
        before: set[Path],
        error_type: str | None,
    ) -> IterationStats:
        """Build IterationStats from this attempt's flushed transcript.

        Finds the transcript that appeared during the run (newest of any new
        files, to tolerate the rare case of more than one), parses its usage,
        and stamps the run's ``error_type``. Falls back to minimal stats when no
        transcript was written — e.g. a session too short to flush, or no
        ``state_dir`` to locate one.
        """
        if config_dir is None:
            return self._stats(error_type)
        new = _transcript_paths(config_dir) - before
        if not new:
            return self._stats(error_type)
        transcript = max(new, key=lambda p: p.stat().st_mtime)
        try:
            stats = transcript_stats(transcript.read_text())
        except OSError:
            return self._stats(error_type)
        stats.error_type = error_type
        if not stats.models and self.model:
            stats.models = [self.model]
        return stats

    def _await_ready(self, child: _PtyProcess) -> bool:
        """Wait until the input box appears, clearing the trust dialog if shown."""
        deadline = time.monotonic() + _READY_TIMEOUT_SEC
        trust_handled = False
        while time.monotonic() < deadline:
            if not child.alive():
                return False
            screen = child.tail()
            if is_auth_error(screen):
                raise AuthenticationError(strip_ansi(screen)[-500:])
            if is_trust_dialog(screen) and not trust_handled:
                trust_handled = True
                logger.debug("ct: trust dialog shown — sending Enter to accept")
                time.sleep(0.4)
                child.send(b"\r")
                time.sleep(1.0)
                continue
            if child.idle_for() > _READY_QUIESCENCE_SEC and is_ready(screen):
                return True
            time.sleep(0.3)
        return False

    def _await_turn_end(
        self, child: _PtyProcess, on_progress: ProgressCallback | None
    ) -> bool:
        """Return True once the turn is over, detected by the pty going quiet.

        Quiescence is the reliable end-of-turn signal: the spinner animates
        continuously while the model thinks or a tool runs, so the pty only stays
        silent for ``_DONE_QUIESCENCE_SEC`` once the turn is actually over. We do NOT
        gate on the on-screen "esc to interrupt" footer — ``tail()`` is the raw,
        cumulative byte stream (no terminal emulation), so a stale frame lingers
        in the window and that footer would never clear.
        """
        deadline = time.monotonic() + _TURN_TIMEOUT_SEC
        saw_activity = False
        last_ping = 0.0
        while time.monotonic() < deadline:
            if not child.alive():
                # Process exited on its own — treat as turn end.
                return True
            screen = child.tail()
            if is_auth_error(screen):
                raise AuthenticationError(strip_ansi(screen)[-500:])
            idle = child.idle_for()
            if idle < 1.0:
                saw_activity = True
            if on_progress is not None and time.monotonic() - last_ping > 5.0:
                last_ping = time.monotonic()
                try:
                    on_progress("working (interactive TUI)…", None)
                except Exception:
                    logger.exception("on_progress raised; continuing")
            if saw_activity and idle > _DONE_QUIESCENCE_SEC:
                return True
            time.sleep(0.5)
        return False

    def _teardown(self, child: _PtyProcess) -> None:
        """Ask the TUI to exit cleanly, then make sure the process is gone."""
        try:
            child.send(b"/exit\r")
        except OSError:
            pass
        for _ in range(20):  # up to ~10s for a graceful exit
            if not child.alive():
                break
            time.sleep(0.5)
        if child.alive():
            child.send(b"\x03\x03")  # Ctrl-C twice
            time.sleep(0.5)
        child.close()
