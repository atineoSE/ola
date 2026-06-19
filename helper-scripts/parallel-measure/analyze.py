#!/usr/bin/env python3
"""Offline staircase analyzer: turn the raw samples into the demo deliverable.

Reads the four sources produced during a staircase run, aligns them on the UTC
clock into per-step windows (from staircase-steps.jsonl), and prints:

  1. The staircase table — one row per concurrency step with aggregate tok/s,
     completions/sec, peak per-core CPU (GIL detector), peak tree-RSS + RAM
     headroom, disk util, and server GPU% / queue depth.
  2. The knee — the step where aggregate tok/s stops rising, and which resource
     saturated there (the answer to Q3).
  3. The Q1 verdict — kill (oom_kill incremented) vs stall (heartbeat went stale
     while tasks were running) vs clean.

Sources (all optional except --steps + --local):
    --steps   .ola/staircase-steps.jsonl   (step windows)
    --local   .ola/local-samples.jsonl     (RAM/CPU/disk/tree-RSS/heartbeat)
    --server  server-samples.jsonl         (GPU% + vLLM queue/throughput)
    --events  .ola/events.jsonl            (aggregate tok/s, completions/sec)

Usage:
    python3 analyze.py --folder /path/to/agent/01-unit-tests
    python3 analyze.py --steps a.jsonl --local b.jsonl --server c.jsonl --events d.jsonl
"""
from __future__ import annotations

import argparse
import calendar
import json
import time
from pathlib import Path


def parse_ts(ts: str) -> float | None:
    """ISO 'YYYY-MM-DDThh:mm:ss(.mmm)?Z' -> epoch seconds (UTC)."""
    if not isinstance(ts, str) or len(ts) < 19:
        return None
    try:
        base = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    frac = 0.0
    if len(ts) > 19 and ts[19] == ".":
        digits = ""
        for ch in ts[20:]:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            frac = float("0." + digits)
    return base + frac


def read_jsonl(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def _max(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(max(xs), 1) if xs else None


def build_windows(steps: list[dict]) -> list[dict]:
    """From step markers, produce steady windows: [step_steady ts, step_end ts] per cap."""
    windows: list[dict] = []
    open_steady: dict[int, float] = {}
    for rec in steps:
        ev = rec.get("event")
        cap = rec.get("cap")
        t = parse_ts(rec.get("ts", ""))
        if t is None or cap is None:
            continue
        if ev == "step_steady":
            open_steady[cap] = t
        elif ev == "step_end" and cap in open_steady:
            windows.append({"cap": cap, "t0": open_steady.pop(cap), "t1": t})
    return windows


def in_window(rows: list[dict], t0: float, t1: float) -> list[dict]:
    out = []
    for r in rows:
        t = parse_ts(r.get("ts", ""))
        if t is not None and t0 <= t <= t1:
            out.append(r)
    return out


def aggregate_tok_s(events: list[dict], t0: float, t1: float) -> tuple[float | None, int]:
    """Sum output tokens produced in [t0,t1] across attempts / window seconds.

    metrics.output_tokens is cumulative per (task_id, attempt). For each attempt
    we take (last - first) observed inside the window; summed and divided by the
    window span gives aggregate output tok/s. Also returns completions in window.
    """
    by_key: dict[tuple, list[tuple[float, int]]] = {}
    completes = 0
    for e in events:
        t = parse_ts(e.get("ts", ""))
        if t is None or not (t0 <= t <= t1):
            continue
        if e.get("status") == "complete":
            completes += 1
        data = e.get("data") or {}
        m = data.get("metrics") or {}
        if "output_tokens" in m:
            key = (e.get("task_id"), e.get("attempt"))
            by_key.setdefault(key, []).append((t, int(m["output_tokens"])))
    span = t1 - t0
    if span <= 0:
        return None, completes
    produced = 0
    for samples in by_key.values():
        samples.sort()
        produced += max(0, samples[-1][1] - samples[0][1])
    return (round(produced / span, 1) if by_key else None), completes


def core_signature(local: list[dict]) -> dict:
    """Per-core CPU summary: peak single core and how many cores were hot.

    One core pegged while others idle == GIL bound (in-process oh). Many cores
    hot == real all-core ceiling. The discriminator for the 'is the knee the
    GIL or the hardware' question.
    """
    peak_single = 0.0
    max_hot = 0           # most cores >85% in any one sample
    samples_seen = 0
    for r in local:
        cpu = r.get("percore_cpu") or {}
        if not cpu:
            continue
        samples_seen += 1
        vals = list(cpu.values())
        peak_single = max(peak_single, max(vals))
        max_hot = max(max_hot, sum(1 for v in vals if v >= 85.0))
    return {"peak_core_pct": round(peak_single, 1) if samples_seen else None,
            "max_cores_hot": max_hot if samples_seen else None}


def summarize_step(w: dict, local: list[dict], server: list[dict], events: list[dict]) -> dict:
    t0, t1 = w["t0"], w["t1"]
    lw = in_window(local, t0, t1)
    sw = in_window(server, t0, t1)

    tok_s, completes = aggregate_tok_s(events, t0, t1)
    span = max(1e-9, t1 - t0)

    mem_cur = [r.get("mem_current") for r in lw]
    mem_max = next((r.get("mem_max") for r in lw if r.get("mem_max")), None)
    tree = [r.get("tree_rss") for r in lw]
    running = [r.get("running") for r in lw]
    disk_peak = _max([max(r["disk_util"].values()) for r in lw if r.get("disk_util")])
    csig = core_signature(lw)

    gpu = [r.get("gpu_util_max") for r in sw]
    waiting = []
    for r in sw:
        srv = r.get("serve") or {}
        if "num_requests_waiting" in srv:
            waiting.append(srv["num_requests_waiting"])

    peak_mem = _max([m for m in mem_cur if m is not None])
    headroom_gb = None
    if peak_mem is not None and mem_max:
        headroom_gb = round((mem_max - peak_mem) / 1e9, 2)

    return {
        "cap": w["cap"],
        "dwell_s": round(span, 0),
        "tok_s": tok_s,
        "completions_per_min": round(completes / span * 60, 1),
        "avg_running": _mean(running),
        "peak_core_pct": csig["peak_core_pct"],
        "max_cores_hot": csig["max_cores_hot"],
        "peak_rss_gb": round(_max(tree) / 1e9, 2) if _max(tree) else None,
        "peak_mem_gb": round(peak_mem / 1e9, 2) if peak_mem else None,
        "ram_headroom_gb": headroom_gb,
        "disk_util_pct": disk_peak,
        "gpu_util_pct": _max(gpu),
        "queue_waiting": _max(waiting),
    }


def detect_knee(rows: list[dict]) -> dict:
    """First step where aggregate tok/s fails to rise >10% over the prior step."""
    prev = None
    for r in rows:
        ts = r.get("tok_s")
        if ts is None:
            continue
        if prev is not None and ts < prev * 1.10:
            return {"knee_cap": r["cap"], "tok_s": ts, "prev_tok_s": prev}
        prev = ts
    return {"knee_cap": None, "note": "tok/s still rising at the top of the staircase"}


def q1_verdict(local: list[dict]) -> dict:
    """Kill vs stall vs clean, from oom_kill counter and heartbeat freshness."""
    ooms = [r.get("oom_kill") for r in local if r.get("oom_kill") is not None]
    oom_delta = (max(ooms) - min(ooms)) if ooms else 0
    # stall: heartbeat age large while tasks were running
    stalled = False
    worst_age = 0.0
    for r in local:
        age = r.get("hb_age_s")
        running = r.get("running") or 0
        if age is not None and running > 0:
            worst_age = max(worst_age, age)
            if age > 30:  # >~6 heartbeat intervals: loop is no longer ticking
                stalled = True
    if oom_delta > 0:
        verdict = f"KILL — oom_kill incremented by {oom_delta} during the run"
    elif stalled:
        verdict = f"STALL — heartbeat went stale ({worst_age:.0f}s) while tasks were running (loop alive? check py-spy)"
    else:
        verdict = "CLEAN — no oom_kill, heartbeat stayed fresh"
    return {"verdict": verdict, "oom_delta": oom_delta, "worst_hb_age_s": round(worst_age, 1)}


def render_table(rows: list[dict]) -> str:
    cols = [
        ("cap", "N"), ("tok_s", "tok/s"), ("completions_per_min", "compl/min"),
        ("avg_running", "running"), ("peak_core_pct", "peak1core%"),
        ("max_cores_hot", "cores_hot"), ("peak_rss_gb", "RSS GB"),
        ("ram_headroom_gb", "RAM free GB"), ("disk_util_pct", "disk%"),
        ("gpu_util_pct", "GPU%"), ("queue_waiting", "srv queue"),
    ]
    head = "| " + " | ".join(h for _, h in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [head, sep]
    for r in rows:
        cells = []
        for k, _ in cols:
            v = r.get(k)
            cells.append("—" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", type=Path, help="agent phase folder; defaults the four source paths under it")
    ap.add_argument("--steps", type=Path)
    ap.add_argument("--local", type=Path)
    ap.add_argument("--server", type=Path)
    ap.add_argument("--events", type=Path)
    ap.add_argument("--json", action="store_true", help="also dump the per-step rows as JSON")
    args = ap.parse_args()

    if args.folder:
        ola = args.folder / ".ola"
        args.steps = args.steps or ola / "staircase-steps.jsonl"
        args.local = args.local or ola / "local-samples.jsonl"
        args.events = args.events or ola / "events.jsonl"
        # server samples usually live on the GPU host; pass --server explicitly

    steps = read_jsonl(args.steps)
    local = read_jsonl(args.local)
    server = read_jsonl(args.server)
    events = read_jsonl(args.events)

    if not steps:
        print(f"ERROR: no step markers in {args.steps} — was staircase.py run?")
        return 1
    if not local:
        print(f"WARN: no local samples in {args.local} — RAM/CPU/disk columns will be blank")

    windows = build_windows(steps)
    if not windows:
        print("ERROR: no complete steady windows (need step_steady + step_end pairs)")
        return 1

    rows = [summarize_step(w, local, server, events) for w in windows]

    print("# Staircase results\n")
    print(f"sources: steps={len(steps)} local={len(local)} server={len(server)} events={len(events)}\n")
    print(render_table(rows))
    print()

    knee = detect_knee(rows)
    if knee.get("knee_cap"):
        kr = next((r for r in rows if r["cap"] == knee["knee_cap"]), {})
        # name the saturated resource at the knee
        reasons = []
        if kr.get("peak_core_pct") and kr["peak_core_pct"] >= 90 and (kr.get("max_cores_hot") or 0) <= 1:
            reasons.append("GIL (one core pegged, others idle)")
        if kr.get("max_cores_hot") and kr["max_cores_hot"] >= 2 and kr.get("peak_core_pct", 0) >= 90:
            reasons.append("all-core CPU")
        if kr.get("ram_headroom_gb") is not None and kr["ram_headroom_gb"] < 1.0:
            reasons.append("RAM (near the no-swap wall — crash risk)")
        if kr.get("disk_util_pct") and kr["disk_util_pct"] >= 90:
            reasons.append("disk")
        if not reasons and (kr.get("gpu_util_pct") or 0) < 80:
            reasons.append("harness serialization (hardware idle, server idle — the embarrassing ceiling)")
        print(f"**Knee:** tok/s plateaus at N={knee['knee_cap']} "
              f"({knee['prev_tok_s']} → {knee['tok_s']} tok/s).")
        print(f"**Saturated at the knee:** {', '.join(reasons) or 'inconclusive — inspect the row'}")
        if (kr.get("gpu_util_pct") or 0) < 80 and (kr.get("queue_waiting") or 0) == 0:
            print("**Demo claim holds:** server GPU < 80% and queue empty at the local knee — "
                  "local-bound while the server has headroom. ✅")
    else:
        print(f"**Knee:** {knee.get('note')}")

    print()
    q1 = q1_verdict(local)
    print(f"**Q1 verdict:** {q1['verdict']}")

    if args.json:
        print("\n```json")
        print(json.dumps({"rows": rows, "knee": knee, "q1": q1}, indent=2))
        print("```")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
