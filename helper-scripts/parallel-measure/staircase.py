#!/usr/bin/env python3
"""Drive the OLA concurrency staircase for a parallel-scaling measurement.

Holds the live concurrency cap (``<folder>/.ola/concurrency``) at each step for a
fixed dwell so the system reaches steady state before the samplers read it, then
advances. The scheduler re-reads the cap every tick, so writing the file *is* the
control knob (this is also a dry run of the dashboard's stepper).

Each transition is logged to a steps JSONL with a UTC timestamp so analyze.py can
slice the sampler streams into per-step windows and read the knee.

Default staircase matches the measurement plan: 2, 4, 8, 16, 24, 32, 48, 64, 80.

Usage:
    python3 staircase.py --folder /work/agent/01-unit-tests \\
        --steps 2,4,8,16,24,32,48,64,80 --dwell 300
    python3 staircase.py --folder ... --dwell 240 --settle 30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"


def write_concurrency(folder: Path, value: int) -> None:
    # tmp + rename, mirroring scheduler.write_concurrency so a reader never sees
    # a half-written cap.
    cap_file = folder / ".ola" / "concurrency"
    cap_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cap_file.with_name(cap_file.name + ".tmp")
    tmp.write_text(f"{value}\n")
    tmp.replace(cap_file)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", required=True, type=Path)
    ap.add_argument("--steps", default="2,4,8,16,24,32,48,64,80",
                    help="comma-separated concurrency caps (default 2,4,8,16,24,32,48,64,80)")
    ap.add_argument("--dwell", type=float, default=300.0, help="seconds to hold each step (default 300)")
    ap.add_argument("--settle", type=float, default=30.0,
                    help="seconds after raising the cap before the step 'steady' window opens "
                         "(logged so analyze.py can exclude the ramp). default 30")
    ap.add_argument("--steps-log", type=Path, help="JSONL step markers (default <folder>/.ola/staircase-steps.jsonl)")
    ap.add_argument("--final", type=int, default=2,
                    help="cap to restore when the staircase finishes or is interrupted (default 2)")
    ap.add_argument("--dry-run", action="store_true", help="print the schedule and exit, writing nothing")
    args = ap.parse_args()

    try:
        steps = [int(s) for s in args.steps.split(",") if s.strip()]
    except ValueError:
        print(f"[staircase] bad --steps: {args.steps!r}", file=sys.stderr)
        return 2
    if not steps:
        print("[staircase] no steps given", file=sys.stderr)
        return 2

    log_path = args.steps_log or (args.folder / ".ola" / "staircase-steps.jsonl")
    total_min = len(steps) * args.dwell / 60.0

    if args.dry_run:
        print(f"[staircase] {len(steps)} steps {steps}, dwell {args.dwell}s, settle {args.settle}s "
              f"-> ~{total_min:.0f} min total")
        return 0

    log_path.parent.mkdir(parents=True, exist_ok=True)

    def mark(event: str, cap: int, **extra) -> None:
        rec = {"ts": _utc_now_iso(), "event": event, "cap": cap, **extra}
        with log_path.open("a", buffering=1) as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        print(f"[staircase] {rec['ts']} {event} cap={cap} {extra or ''}", file=sys.stderr)

    print(f"[staircase] folder={args.folder} steps={steps} dwell={args.dwell}s "
          f"~{total_min:.0f} min -> {log_path}", file=sys.stderr)
    mark("run_start", steps[0], steps=steps, dwell=args.dwell, settle=args.settle)
    try:
        for cap in steps:
            write_concurrency(args.folder, cap)
            mark("step_set", cap)              # cap raised; system ramping
            if args.settle > 0:
                time.sleep(min(args.settle, args.dwell))
            mark("step_steady", cap)           # steady window opens (sample between here and step_end)
            remaining = args.dwell - min(args.settle, args.dwell)
            if remaining > 0:
                time.sleep(remaining)
            mark("step_end", cap)              # steady window closes
        mark("run_end", steps[-1])
    except KeyboardInterrupt:
        print("\n[staircase] interrupted — restoring cap", file=sys.stderr)
        mark("interrupted", args.final)
    finally:
        write_concurrency(args.folder, args.final)
        print(f"[staircase] restored concurrency -> {args.final}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
