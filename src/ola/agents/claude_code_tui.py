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
* **Global stops come off the transcript, not the screen.** A dead credential
  and an exhausted subscription window are global — one shared resource behind
  every task — so each aborts the whole run rather than failing task-by-task
  (``cc`` does the same from its stream; see ``error_type`` in
  ``claude_code.py``). The TUI publishes no stdout stream, but it *does* append
  a machine-readable record to the session transcript the moment a request
  fails, and it appends it **live**, not at exit: ``isApiErrorMessage: true``
  with an ``error`` field (``rate_limit`` / ``authentication_failed``), an
  ``apiErrorStatus``, and — for a limit — a ``quotaLimits`` block carrying the
  very same ``status``/``resetsAt``/``rateLimitType`` payload ``cc`` reads off
  its ``rate_limit_event``. :class:`_TranscriptWatcher` tails that file during
  the turn, so both stops are read from a *structured* wire.
  The screen stays the wire only for state that never reaches an API call and
  so leaves no record: the trust dialog, the ready box, and a credential dead
  enough that the TUI never opens a session at all. That boundary is
  structural, not a preference — where a transcript exists it is the wire, and
  before one exists there is nothing else to read — so the two can never
  disagree about the same event.
  Neither stop can wait for quiescence: a limited turn goes silent at once,
  which the end-of-turn heuristic below would read as a finished turn that
  simply did not tick — i.e. stagnation, and every task burning its attempts
  against an unmoved wall.
* **A usage limit is waited out here, not escalated** — the one place ``ct``
  diverges from ``cc``. The interactive CLI does not kill a limited turn the
  way ``claude -p`` does: the session and its context stay live across the
  window, so restarting would throw them away to re-derive a plan that has not
  changed. :meth:`_park_for_limit` holds the turn until the record's
  ``quotaLimits.resetsAt`` — an epoch the CLI states outright, so there is
  nothing to parse and nothing to guess. Only a record without one falls back
  to the next five-hour window boundary (:func:`next_window_boundary`). So this
  backend never raises a rate-limit escalation: ``error_type="rate_limited"``
  and exit 41 are ``cc``'s alone.
* **ola restarts the parked turn itself** (:meth:`_nudge_after_limit`) rather
  than trusting the CLI to. The CLI does have an auto-continue — it says
  "continuing automatically at 4pm" and re-sends the turn when the window
  reopens — but arming it requires the ``autoContinueAtUsageLimit`` setting
  *and* a server-delivered config ola can neither read nor set, and its
  no-dialog arm is skipped outright in background/job contexts. On 2026-09-02
  it did not fire: the session sat at the prompt until a human typed
  "continue". ola does not try to tell the two cases apart — a nudge that was
  not needed costs one queued prompt, a nudge that was skipped costs
  :data:`_TURN_TIMEOUT_SEC`.
* **Metrics, post-turn.** The same transcript the watcher tails —
  ``<CLAUDE_CONFIG_DIR>/projects/<slug>/<session>.jsonl`` — is where the usage
  blocks land. It is appended as the session runs, but metrics are read *after*
  teardown so the last turn's records are certainly in it. From it we recover
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
from dataclasses import dataclass
from datetime import datetime
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
# Authentication before a session exists is the one stop with no transcript to
# read: a credential dead enough that the TUI never opens a session writes no
# record, because it never makes a request. That is why _AUTH_MARKERS above are
# consulted only while waiting for the ready box — see _await_ready. Once a
# turn is running, the same failure arrives as a structured record instead
# (error: "authentication_failed"), and _TranscriptWatcher is the only reader.

# A reset further out than this is not a five-hour window — most likely a
# weekly cap, which the TUI has never been observed to sit through. Parking a
# worktree, a sandbox slot and a thread for days on one is worse than waking at
# the next boundary and finding out: the turn simply re-parks on the next
# record. The cap also bounds a nonsense epoch, though the CLI states this one
# outright and has never sent a bad one.
_MAX_PARK_SEC = 6 * 3600

# Subscription windows are five hours long and, for this account, fall on a
# boundary at 18:00 Europe/Madrid — so 18:00, 23:00, 04:00, 09:00, 14:00 local.
# Used only for a limit record that carries no usable ``resetsAt`` — never
# observed, since all 34 captured records state one. A derived boundary is a
# good guess, never better than what the CLI actually said.
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
# Pasted once the window reopens, to restart the turn the limit interrupted.
# Sent unconditionally: whether the CLI would have resumed by itself depends on
# a server-side flag ola cannot observe (see the module docstring), and
# "did it resume?" is only answerable from silence — the same signal the
# end-of-turn heuristic reads, which is exactly what must not be trusted here.
# Sending it when it was not needed is self-correcting in both directions: the
# CLI's own banner offers "esc or type to cancel", so typing cancels a still-
# armed auto-continue and submits this instead (same outcome), and a nudge sent
# before the window really reopened is rejected, writes another limit record,
# and re-parks through the caller's ordinary loop.
_RESUME_NUDGE = (
    "The usage limit has reset. Continue the task you were working on when the "
    "limit interrupted you; do not repeat work that is already complete."
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


@dataclass(frozen=True)
class _ApiError:
    """One ``isApiErrorMessage`` record from the live session transcript.

    This is ``ct``'s wire for the two global stops. The TUI renders both as
    prose in a banner, but writes them here as fields — and prose is what the
    CLI is free to rewrite between releases: the "You've hit your session
    limit" wording matched no screen marker for a whole run of limited turns,
    each of which then read as an agent that finished without ticking.

    ``quotaLimits`` is the same payload ``cc`` gets from its ``rate_limit_event``
    (``status``/``resetsAt``/``rateLimitType``), which is what makes ``resetsAt``
    an epoch to park on rather than an hour to parse out of a sentence.
    """

    error: str
    text: str
    status: int | None = None
    quota_status: str | None = None
    limit_type: str | None = None
    resets_at: float | None = None

    @property
    def is_rate_limit(self) -> bool:
        return self.error == "rate_limit"

    @property
    def is_auth_failure(self) -> bool:
        return self.error == "authentication_failed"

    def describe(self) -> str:
        """One-line log form — the fields, not the prose, so a shape change shows."""
        bits = [f"error={self.error}", f"status={self.status}"]
        if self.quota_status:
            bits.append(f"quota={self.quota_status}")
        if self.limit_type:
            bits.append(f"type={self.limit_type}")
        return f"{' '.join(bits)}: {self.text}"


def _record_text(record: dict) -> str:
    """The human-readable text of an error record, for logs and messages."""
    content = ((record.get("message") or {}).get("content")) or []
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(t for t in parts if t).strip()
    return str(content)[:200]


def parse_api_error(line: str | bytes) -> _ApiError | None:
    """Read one transcript line as an API-error record, or None.

    Only records the CLI itself flags with ``isApiErrorMessage`` count: an
    ordinary assistant message that happens to *discuss* a rate limit (an agent
    writing one into a test fixture, say) carries no such flag, which is a
    distinction no amount of screen-matching could make.
    """
    if isinstance(line, bytes):
        if b"isApiErrorMessage" not in line:
            return None
        text = line.decode("utf-8", "replace")
    else:
        if "isApiErrorMessage" not in line:
            return None
        text = line
    try:
        record = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("isApiErrorMessage"):
        return None
    quota = record.get("quotaLimits")
    quota = quota if isinstance(quota, dict) else {}
    resets = quota.get("resetsAt")
    status = record.get("apiErrorStatus")
    return _ApiError(
        error=str(record.get("error") or ""),
        text=_record_text(record),
        status=status if isinstance(status, int) else None,
        quota_status=quota.get("status"),
        limit_type=quota.get("rateLimitType"),
        resets_at=float(resets) if isinstance(resets, (int, float)) else None,
    )


class _TranscriptWatcher:
    """Tails this attempt's transcript(s) for API-error records, live.

    The TUI appends to ``projects/<slug>/<session>.jsonl`` as the turn runs, so
    a record lands about half a second after the request that failed — well
    inside the quiescence window the end-of-turn heuristic waits out, which is
    the whole reason a limit can be caught before it is mistaken for a finished
    turn.

    Every *new* file under the config dir is followed, not just the newest one:
    picking a single session file would mean picking it before the CLI has
    created it, and a wrong pick loses the only copy of the event. Each file
    keeps its own byte offset, and only whole lines are consumed, so a record
    still being written is read on the next poll rather than half-parsed.

    Scoped to :func:`_transcript_paths`, so a *sub-agent's* transcript is not
    watched, for the same reason it is not counted in metrics — it is not this
    task's session. A limit that rejects a sub-agent's request rejects the main
    session's next one too, which is the record this reads.
    """

    def __init__(self, config_dir: Path | None, before: set[Path]):
        self._config_dir = config_dir
        self._before = before
        self._offsets: dict[Path, int] = {}

    def poll(self) -> list[_ApiError]:
        """Return error records appended since the last call (oldest first)."""
        if self._config_dir is None:
            return []
        events: list[_ApiError] = []
        try:
            paths = sorted(_transcript_paths(self._config_dir) - self._before)
        except OSError:
            return events
        for path in paths:
            offset = self._offsets.get(path, 0)
            try:
                if path.stat().st_size < offset:
                    offset = 0  # truncated/replaced — start over
                with path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
            except OSError:
                continue
            cut = chunk.rfind(b"\n")
            if cut == -1:
                continue  # nothing but a partial line yet
            self._offsets[path] = offset + cut + 1
            for line in chunk[:cut].split(b"\n"):
                event = parse_api_error(line)
                if event is not None:
                    events.append(event)
        return events



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
        # The same snapshot serves the live watcher: this attempt's transcript
        # is whatever appears under the config dir that was not there before.
        # Without a config dir there is no transcript and therefore no wire —
        # ola always passes one (``state_dir_name = ".claude"``), so this is a
        # bare-call shape, but say so rather than fail a limit silently.
        watcher = _TranscriptWatcher(config_dir, before)
        if config_dir is None:
            logger.warning(
                "ct: no state_dir — a usage limit or auth failure mid-turn "
                "cannot be detected (no transcript to read)."
            )

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
            if not self._await_ready(child, watcher):
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
                done = self._await_turn_end(child, watcher, on_progress)
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

        # Reaching here means nothing stopped the turn — it ended normally, or
        # timed out. Log the tail anyway: a stop this backend fails to
        # recognise leaves no other trace, because such a turn reads as
        # finished-but-unticked and the scheduler drops ``output`` on that
        # path, so the evidence would cost another full window to reproduce.
        # That is not hypothetical — it is exactly how the "You've hit your
        # session limit" wording was found (2026-09-02) while detection was
        # still screen-scraped. Detection has moved to the transcript since,
        # which narrows what this can catch but does not close it: the record
        # shape is no more a public API than the prose was.
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

    def _await_ready(self, child: _PtyProcess, watcher: _TranscriptWatcher) -> bool:
        """Wait until the input box appears, clearing the trust dialog if shown."""
        deadline = time.monotonic() + _READY_TIMEOUT_SEC
        trust_handled = False
        while time.monotonic() < deadline:
            if not child.alive():
                return False
            screen = child.tail()
            # The one place the *screen* still carries a global stop: a
            # credential this dead never reaches a request, so it never writes
            # a record. There is no transcript yet to disagree with.
            if is_auth_error(screen):
                raise AuthenticationError(strip_ansi(screen)[-500:])
            # Same watcher as the turn loop, in case the CLI ever makes a
            # request before the prompt is pasted (every captured limit record
            # so far follows one, so this has not been observed firing).
            deadline += self._handle_api_errors(child, watcher, None)
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
        self,
        child: _PtyProcess,
        watcher: _TranscriptWatcher,
        on_progress: ProgressCallback | None,
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
            # Must be handled here, not left to quiescence: a parked turn goes
            # silent, so _DONE_QUIESCENCE_SEC elapses and this would return True
            # — an agent that "finished" without ticking its checkbox, which is
            # stagnation to the scheduler and burns every task's attempts.
            waited = self._handle_api_errors(child, watcher, on_progress)
            if waited:
                deadline += waited
                self._nudge_after_limit(child)
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

    def _handle_api_errors(
        self,
        child: _PtyProcess,
        watcher: _TranscriptWatcher,
        on_progress: ProgressCallback | None,
    ) -> float:
        """React to error records the TUI appended; return seconds parked.

        One reader for both global stops, because they arrive on one wire in
        one shape and differ only by a field. Anything else the CLI flags is
        logged and left alone: an unhandled shape showing up in the log is how
        the next missing reaction gets found, and reacting to one ola has no
        answer for would be worse than letting the turn run.
        """
        waited = 0.0
        for event in watcher.poll():
            if event.is_auth_failure:
                # Global: every task shares this credential, so the scheduler
                # aborts the run instead of requeueing this one.
                raise AuthenticationError(event.text or event.describe())
            if event.is_rate_limit:
                logger.debug("ct: limit record: %s", event.describe())
                waited += self._park_for_limit(child, event, watcher, on_progress)
                continue
            logger.debug("ct: transcript API error, no ola reaction: %s", event.describe())
        return waited

    def _reset_epoch(self, event: _ApiError) -> float:
        """When the window reopens, per the record — or the derived boundary.

        The CLI states ``resetsAt`` outright, so there is nothing to parse. The
        fallback covers a record that carries none, or one further out than a
        five-hour window (see _MAX_PARK_SEC): waking at the next boundary and
        re-parking on the next record beats holding a worktree for days.
        """
        reset_at = event.resets_at
        if reset_at is not None and 0 < reset_at - time.time() <= _MAX_PARK_SEC:
            return reset_at
        boundary = next_window_boundary()
        logger.warning(
            "ct: usage limit with no usable resetsAt (%s) — falling back to the "
            "next %dh window boundary.",
            event.describe(),
            _WINDOW_HOURS,
        )
        return boundary

    def _park_for_limit(
        self,
        child: _PtyProcess,
        event: _ApiError,
        watcher: _TranscriptWatcher,
        on_progress: ProgressCallback | None,
    ) -> float:
        """Hold a turn the TUI has parked on a usage limit; return seconds waited.

        The interactive CLI does not kill a limited turn the way ``claude -p``
        does — the session and the context it has built survive the window. So
        killing the turn to let ``ola-monitor`` relaunch would discard live work
        and re-derive a plan that has not changed; waiting keeps it.

        This is a deliberate exception to "ola never waits out a window", not a
        forgotten one. That rule exists because a ``cc`` turn the limit rejects
        is *dead* — there is no in-flight work to protect, so parking a process
        buys nothing. Here the work is alive and waiting. The distinguishing
        signal is the CLI reporting the rejection itself, not a threshold ola
        picked, so there is still one reaction per condition.

        Waiting is all this does. Getting the turn *moving* again is the
        caller's, because only the turn loop has a turn to restart:
        :meth:`_await_ready` parks on the very same records and then resumes by
        pasting the prompt it was always going to paste, and a nudge there would
        land in the input box ahead of it.
        """
        reset_at = self._reset_epoch(event)
        wait = max(0.0, reset_at - time.time()) + _PARK_GRACE_SEC
        resumes = datetime.fromtimestamp(reset_at).strftime("%H:%M")
        logger.warning(
            "ct: usage limit (%s) — window reopens at %s local (%.0f min); "
            "holding the turn rather than restarting it.",
            event.limit_type or "unknown window",
            resumes,
            wait / 60,
        )

        started = time.monotonic()
        while time.monotonic() - started < wait:
            if not child.alive():
                # The TUI died while parked; let the caller's liveness check
                # treat it as end-of-turn rather than sleeping out the window.
                break
            # Keep reading the wire while parked. A credential that dies during
            # the wait must still escalate, and a *second* limit record (the
            # window reopened, the turn resumed, and it ran out again) extends
            # the wait rather than being swallowed — the record is consumed
            # from the transcript, so nobody else will see it.
            for later in watcher.poll():
                if later.is_auth_failure:
                    raise AuthenticationError(later.text or later.describe())
                if not later.is_rate_limit:
                    continue
                next_reset = self._reset_epoch(later)
                if next_reset > reset_at:
                    reset_at = next_reset
                    wait = (
                        max(0.0, reset_at - time.time())
                        + _PARK_GRACE_SEC
                        + (time.monotonic() - started)
                    )
                    resumes = datetime.fromtimestamp(reset_at).strftime("%H:%M")
                    logger.warning(
                        "ct: still limited — extending the park to %s local.",
                        resumes,
                    )
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

        logger.info("ct: usage-limit window reopened.")
        return time.monotonic() - started

    def _nudge_after_limit(self, child: _PtyProcess) -> None:
        """Ask the TUI to pick the turn back up now the window has reopened.

        See :data:`_RESUME_NUDGE` for why this is unconditional. Skipped only
        when the TUI died while parked — there the caller's liveness check ends
        the turn, and pasting into a dead pty would just raise.
        """
        if not child.alive():
            return
        logger.info("ct: nudging the TUI to resume the turn the limit stopped.")
        _paste(child, _RESUME_NUDGE)

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
