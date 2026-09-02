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
* **Global stops come off the screen.** A dead credential and an exhausted
  subscription window are global — one shared resource behind every task — so
  each aborts the whole run rather than failing task-by-task (``cc`` does the
  same from its stream; see ``error_type`` in ``claude_code.py``). The TUI
  publishes no machine-readable stream, so both are detected as screen banners
  (``is_auth_error`` / ``is_rate_limited``), one detector each. Neither can wait
  for quiescence: a limited turn goes silent at once, which the end-of-turn
  heuristic below would read as a finished turn that simply did not tick — i.e.
  stagnation, and every task burning its attempts against an unmoved wall.
* **A usage limit is waited out here, not escalated** — the one place ``ct``
  diverges from ``cc``. The interactive CLI does not kill a limited turn: it
  says "continuing automatically at 4pm" and resumes by itself, holding the
  session and its context. Restarting it would throw that away to re-derive a
  plan that has not changed, so :meth:`_park_for_limit` holds the turn until the
  reset the banner states (:func:`parse_reset_at` reads it out of the prose).
  When the reset time will not parse it falls back to the next five-hour window
  boundary (:func:`next_window_boundary`). So this backend never raises a
  rate-limit escalation: ``error_type="rate_limited"`` and exit 41 are ``cc``'s
  alone.
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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from ola.agents.base import AgentResponse, ProgressCallback
from ola.agents.claude_code import (
    AuthenticationError,
    ClaudeCodeAgent,
    _clear_shadowing_keychain_entry,
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
# settings.json is refreshed every run for the same reason as in ``cc`` — ola
# generates it, so the per-task copy is a cache, not state. See claude_code.py.
_ALWAYS_REFRESH = {".credentials.json", "settings.json"}


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
# Subscription-limit banners. The TUI says the same thing two ways — the window
# "reached", or "you've hit" it — so this is one alternation over both, and
# every branch carries a *stop* verb. That is what keeps the warnings out: the
# hint ("Approaching usage limit · /model to use best available") and the meter
# ("You've used 93% of your session limit · resets 11:10am (UTC)") name a limit
# without hitting one, and the CLI keeps working on the fallback model. The
# separator glyph before the reset time varies (· vs ∙), so no branch spans it.
#
# Both families are pinned to real captures: "Usage limit reached · continuing
# automatically at 4pm" (2026-08-25, LIMIT_SCREEN_REAL in the tests) and
# "You've hit your session limit · resets 11:10am (UTC)" (2026-09-02,
# LIMIT_SCREEN_SESSION). The second one is why the alternation exists: while
# every branch required "reached", a whole run of limited turns went undetected
# — each read as an agent that finished without ticking, i.e. stagnation, and
# burned its attempts against the wall this path exists to wait out. The
# 2026-08-25 capture had already carried an unmatched "your session limit"
# copy; leaving it unpinned cost that run.
#
# The noun after "reached/hit your" tracks whichever window ran out
# (session/usage/weekly/…), so it is a bounded wildcard rather than an
# enumeration of wordings — anchoring on the verb, not the noun, is what
# separates a stop from a warning. Keep the end-of-turn screen logging in
# _run_tui: it is the only trace a still-unknown third wording would leave.
_LIMIT_RE = re.compile(
    r"usagelimitreached|hourlimitreached|(?:reached|hit)your[a-z0-9]{0,10}limit"
)

# "…resets 4pm (UTC)", "…continuing automatically at 4pm", "…at 3:30pm".
# Matched against compact() output, so no whitespace and the parenthesised
# timezone (when present) runs straight into the time. The banner states the
# same time more than once and the pty tail garbles early copies (dropped
# characters — see _await_turn_end), so every match is collected and the one
# carrying a timezone wins; a garbled copy simply fails to match.
_RESET_RE = re.compile(
    r"(?:resets|automaticallyat|at)(\d{1,2})(?::(\d{2}))?(am|pm)\(?([a-z]{2,4})?\)?"
)

# A parsed reset further out than this is a misparse, not a real window — the
# longest subscription window is five hours. Falling back to None escalates,
# which is the safe direction: ola-monitor waits instead, rather than this
# process parking for a day on a bad regex hit.
_MAX_PARK_SEC = 6 * 3600

# Subscription windows are five hours long and, for this account, fall on a
# boundary at 18:00 Europe/Madrid — so 18:00, 23:00, 04:00, 09:00, 14:00 local.
# Used only when the banner's own reset time cannot be read: a derived boundary
# is a good guess, never better than what the CLI actually said.
#
# This is an empirical observation about one account, not a documented API. The
# window is *rolling* — anchored to the first message after an idle stretch — so
# the phase can drift, and this constant is where to correct it when it does.
# Drift is survivable by construction: waking early is harmless (the park resets
# ``saw_activity``, so the turn cannot end until the TUI actually produces
# output again), and waking late only wastes idle time.
# The grid is continuous from one *observed* boundary, not re-derived daily:
# 24 hours is not a multiple of 5, so the wall-clock hour shifts by an hour each
# day and any "every day at HH:00" formulation is wrong within a day. Stepping
# on the epoch also keeps a window five real hours long across a DST change.
_WINDOW_HOURS = 5
_WINDOW_ANCHOR_LOCAL = "2026-08-25 18:00"
_WINDOW_ANCHOR_TZ = "Europe/Madrid"

# Sleep a little past the stated reset: the banner states the reset to the
# minute, and coming back a few seconds early just re-parks.
_PARK_GRACE_SEC = 30.0
# Poll cadence while parked — long enough to be free, short enough that a dead
# TUI or a dead credential is noticed in seconds rather than at the reset.
_PARK_POLL_SEC = 10.0


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


def is_rate_limited(screen: str) -> bool:
    return _LIMIT_RE.search(compact(screen)) is not None


def next_window_boundary(now: float | None = None) -> float:
    """Next five-hour subscription-window boundary, as epoch seconds.

    The fallback for a banner whose reset time will not parse. Five-hour windows
    stepping from one boundary this account was observed to hit
    (:data:`_WINDOW_ANCHOR_LOCAL`, the reset in the 2026-08-25 capture).
    """
    now = time.time() if now is None else now
    anchor = datetime.strptime(_WINDOW_ANCHOR_LOCAL, "%Y-%m-%d %H:%M").replace(
        tzinfo=ZoneInfo(_WINDOW_ANCHOR_TZ)
    )
    step = _WINDOW_HOURS * 3600
    # Floor division walks to the boundary at or before *now* (correct for
    # instants before the anchor too, where the quotient is negative), then one
    # step forward gives the next one strictly ahead.
    elapsed = now - anchor.timestamp()
    return anchor.timestamp() + (elapsed // step + 1) * step


def parse_reset_at(screen: str, now: float | None = None) -> float | None:
    """Epoch seconds the limit banner says the window reopens, or None.

    The TUI states the reset as prose ("resets 4pm (UTC)", "continuing
    automatically at 4pm") rather than as an epoch, but prose is not the same as
    unusable — and it is the difference between one relaunch and twenty. Returns
    None when nothing parses, which escalates instead: a wrong epoch is worse
    than no epoch, so every uncertain case degrades to the old behaviour.

    A bare hour with no timezone is read as local, which is how the TUI renders
    it when it does not say otherwise.
    """
    text = compact(screen)
    matches = _RESET_RE.findall(text)
    if not matches:
        return None
    # The banner repeats the time; prefer a copy carrying an explicit timezone,
    # since that one needs no assumption about the host's clock.
    hour_s, minute_s, meridiem, tz = next(
        (m for m in matches if m[3] in ("utc", "gmt")), matches[0]
    )
    hour = int(hour_s)
    if hour > 12:
        return None
    minute = int(minute_s) if minute_s else 0
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0

    now = time.time() if now is None else now
    utc = tz in ("utc", "gmt")
    base = datetime.fromtimestamp(now, tz=timezone.utc) if utc else datetime.fromtimestamp(now)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    reset = target.timestamp()
    # Guard a misparse rather than parking on it — see _MAX_PARK_SEC.
    return reset if reset - now <= _MAX_PARK_SEC else None


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
                # Same per-task config dir shape as ``cc``, so the same macOS
                # trap: the Keychain entry keyed to this dir outranks the
                # .credentials.json just copied into it, and the dir is stable
                # across runs — so one expired token poisons this task id
                # permanently. See _clear_shadowing_keychain_entry.
                _clear_shadowing_keychain_entry(sd)
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

        # Reaching here means no banner matched — the turn ended normally, or
        # timed out. Log the tail anyway: the screen is this backend's only
        # wire, and a *missed* limit banner leaves no other trace, because such
        # a turn reads as finished-but-unticked and the scheduler drops
        # ``output`` on that path. Without this the evidence costs another full
        # window to reproduce. This is exactly how the "You've hit your session
        # limit" wording was found (2026-09-02) after it slipped past every
        # marker; keep it while any wording of _LIMIT_RE stays unobserved.
        logger.debug("ct: end-of-turn screen tail:\n%s", output[-1000:])

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
        parked = False  # one park per wait; see _await_turn_end
        while time.monotonic() < deadline:
            if not child.alive():
                return False
            screen = child.tail()
            if is_auth_error(screen):
                raise AuthenticationError(strip_ansi(screen)[-500:])
            # The limit banner is usually already up here, before the prompt is
            # ever pasted — the 2026-08-25 capture hit this branch five seconds
            # after spawn, not the mid-turn one. Park the same way: the TUI is
            # going to sit there until the window reopens either way.
            if not parked and is_rate_limited(screen):
                parked = True
                deadline += self._park_for_limit(child, screen, None)
                continue
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
        # One park per turn. The banner never leaves ``tail()`` once printed —
        # the tail is the raw cumulative byte stream, so a stale frame lingers
        # (the same reason the "esc to interrupt" footer is not gated on) — so
        # re-testing the marker after the park would re-park forever.
        parked = False
        while time.monotonic() < deadline:
            if not child.alive():
                # Process exited on its own — treat as turn end.
                return True
            screen = child.tail()
            if is_auth_error(screen):
                raise AuthenticationError(strip_ansi(screen)[-500:])
            # Must be handled here, not left to quiescence: a parked turn goes
            # silent, so _DONE_QUIESCENCE_SEC elapses and this would return True
            # — an agent that "finished" without ticking its checkbox, which is
            # stagnation to the scheduler and burns every task's attempts.
            if not parked and is_rate_limited(screen):
                parked = True
                deadline += self._park_for_limit(child, screen, on_progress)
                # Forget any activity seen before the limit hit. The turn may
                # only end on output the TUI produces *after* resuming, so a
                # park that wakes early (a derived boundary can be off) just
                # keeps polling instead of reading the parked silence as a
                # finished turn — the stagnation bug this whole path exists to
                # avoid.
                saw_activity = False
                continue
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

    def _park_for_limit(
        self,
        child: _PtyProcess,
        screen: str,
        on_progress: ProgressCallback | None,
    ) -> float:
        """Hold a turn the TUI has parked on a usage limit; return seconds waited.

        The interactive CLI does not kill a limited turn the way ``claude -p``
        does — it says "continuing automatically at 4pm" and resumes by itself
        when the window reopens, keeping the session and its context. So the
        cheapest correct thing ola can do is nothing: killing the turn to let
        ``ola-monitor`` relaunch would discard context the CLI is still holding
        and re-derive a plan that has not changed.

        This is a deliberate exception to "ola never waits out a window", not a
        forgotten one. That rule exists because a ``cc`` turn the limit rejects
        is *dead* — there is no in-flight work to protect, so parking a process
        buys nothing. Here the work is alive and waiting. The distinguishing
        signal is the CLI stating its own intent on screen, not a threshold ola
        picked, so there is still one reaction per condition.

        Escalates instead (unchanged behaviour) when the reset time cannot be
        read: parking for an unknown duration is the one case where stopping and
        letting the supervisor wait really is better.
        """
        reset_at = parse_reset_at(screen)
        if reset_at is None:
            # The banner said something we could not read. Windows are five
            # hours on a known grid, so a derived boundary beats both guessing
            # and giving up — and being wrong is survivable in a way it is not
            # elsewhere: waking early costs a busier poll (the caller clears
            # ``saw_activity``, so the turn cannot end until the TUI really
            # resumes), waking late costs idle time bounded by one window.
            reset_at = next_window_boundary()
            logger.warning(
                "ct: usage limit reached but no reset time on screen — "
                "falling back to the next %dh window boundary.",
                _WINDOW_HOURS,
            )

        wait = max(0.0, reset_at - time.time()) + _PARK_GRACE_SEC
        resumes = datetime.fromtimestamp(reset_at).strftime("%H:%M")
        logger.warning(
            "ct: usage limit reached — the TUI resumes this turn by itself at "
            "%s local (%.0f min); holding rather than restarting it.",
            resumes,
            wait / 60,
        )

        started = time.monotonic()
        while time.monotonic() - started < wait:
            if not child.alive():
                # The TUI died while parked; let the caller's liveness check
                # treat it as end-of-turn rather than sleeping out the window.
                break
            if is_auth_error(child.tail()):
                raise AuthenticationError(strip_ansi(child.tail())[-500:])
            if on_progress is not None:
                remaining = (wait - (time.monotonic() - started)) / 60
                try:
                    on_progress(
                        f"usage limit — resuming at {resumes} (~{remaining:.0f} min)",
                        None,
                    )
                except Exception:
                    logger.exception("on_progress raised; continuing")
            time.sleep(_PARK_POLL_SEC)

        logger.info("ct: usage-limit window reopened; turn continuing.")
        return time.monotonic() - started

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
