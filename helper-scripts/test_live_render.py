#!/usr/bin/env python3
"""Verify that Rich's Live widget overwrites frames correctly.

Simulates multiple Live update cycles, captures the raw ANSI output, and checks
that each frame transition has enough cursor-up sequences to fully overwrite the
previous frame. A mismatch indicates duplicate/stale rows will appear on screen.

Run with: PYTHONPATH=src python helper-scripts/test_live_render.py
"""

from __future__ import annotations

import io
import re
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live

from ola.monitor.data import FolderStatus, IterationStatus
from ola.monitor.ui import build_table

FIXTURES = [
    FolderStatus(
        name="frontend",
        tasks_completed=3,
        tasks_total=5,
        iterations=[
            IterationStatus(
                phase="seed",
                wall_ms=45_000,
                input_tokens=12_500,
                output_tokens=3_200,
                cache_read_tokens=8_000,
            ),
        ],
    ),
    FolderStatus(
        name="backend-api",
        tasks_completed=7,
        tasks_total=7,
        iterations=[
            IterationStatus(
                phase="seed",
                wall_ms=120_000,
                input_tokens=250_000,
                output_tokens=45_000,
                cache_read_tokens=180_000,
            ),
        ],
    ),
]

agent_path = Path("/tmp/test-agent")


def run_live_capture(num_updates: int = 3) -> str:
    buf = io.StringIO()
    console = Console(
        file=buf, width=100, force_terminal=True, color_system="truecolor"
    )
    table = build_table(FIXTURES, set(), 0, agent_path)
    with Live(table, console=console, refresh_per_second=4) as live:
        for _ in range(num_updates):
            time.sleep(0.3)
            live.update(build_table(FIXTURES, set(), 0, agent_path))
    return buf.getvalue()


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[^a-zA-Z]*[a-zA-Z]", "", text)


if __name__ == "__main__":
    raw = run_live_capture(3)

    # Check cursor-up counts
    first_frame_end = raw.find("\r\x1b[2K\x1b[1A")
    first_frame = raw[:first_frame_end]
    line_count = first_frame.count("\n")

    between_frames = re.findall(r"(\r(?:\x1b\[2K\x1b\[1A)+\x1b\[2K)", raw)
    all_ok = True
    for i, seq in enumerate(between_frames):
        ups = seq.count("\x1b[1A")
        if ups < line_count:
            print(f"FAIL: Frame {i + 2} only has {ups} cursor-ups, need {line_count}")
            all_ok = False

    if all_ok:
        print(
            f"OK: All {len(between_frames)} frame transitions have {line_count} cursor-ups"
        )

    # Simulate what a real terminal would show: apply cursor-up and clear-line
    # The final visible output should have exactly 1 "Folder" header
    # Strip everything between \r\x1b[2K...\x1b[2K and the start of the re-render
    # In a real terminal, cursor-up + clear means old frames are erased
    # So only the LAST frame should be visible
    clean = strip_ansi(raw)
    # After stripping ANSI, count visible "Folder" strings
    # In a real terminal only the last frame would be visible, but in our capture all frames
    # are in the buffer. The key metric is: are cursor-up counts correct?
    print(
        f"Total 'Folder' in buffer: {raw.count('Folder')} (expected: 1 initial + {len(between_frames)} refreshes + 1 final)"
    )
    print()
    print("If cursor-up counts match line counts, the terminal will correctly")
    print("overwrite each frame — no duplicate headers visible to the user.")
    print()

    # Final: verify bare \n is still used (that's fine — the TERMINAL translates it with OPOST on)
    has_bare_lf = bool(re.search(r"(?<!\r)\n", raw))
    print(
        f"Uses bare \\n: {has_bare_lf} (OK — terminal OPOST translates to \\r\\n in cbreak mode)"
    )
