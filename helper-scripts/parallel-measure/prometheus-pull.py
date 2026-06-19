#!/usr/bin/env python3
"""Pull the remote (server) half of the staircase from Prometheus.

The self-hosted stack already runs Prometheus (loopback :9090) scraping *both*
vLLM (job ``vllm``) and the GPU via DCGM (job ``dcgm``). So instead of running a
live sampler on the GPU host, we query Prometheus' range API after the run and
emit JSONL in the exact schema analyze.py's ``--server`` expects — with full
history and a server-side UTC clock.

Reach Prometheus either way:
  * SSH tunnel:  ssh -fNL 9090:127.0.0.1:9090 -i ~/.ssh/serenity_coding.pem \\
                     substrate@code.adriantineo.com
                 then --prom-url http://localhost:9090
  * or point --prom-url at any reachable Prometheus.

Window: pass --steps <staircase-steps.jsonl> to auto-derive [first..last] marker
time (padded), or give --start/--end explicitly (ISO-8601 or epoch seconds).

Emits one row per step-resolution tick:
  {"ts": "...Z", "gpu_util_max": <%>, "serve": {"num_requests_running": n,
   "num_requests_waiting": n, "gen_tok_s": <tok/s>}}

Usage:
  python3 prometheus-pull.py --prom-url http://localhost:9090 \\
      --steps .../.ola/staircase-steps.jsonl --out server-samples.jsonl
"""
from __future__ import annotations

import argparse
import calendar
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

# query label -> PromQL. Aggregations collapse multi-GPU / multi-engine series to
# one number per tick so analyze.py reads a single value.
QUERIES = {
    "gpu_util_max": "max(DCGM_FI_DEV_GPU_UTIL)",
    "num_requests_running": "sum(vllm:num_requests_running)",
    "num_requests_waiting": "sum(vllm:num_requests_waiting)",
    "gen_tok_s": "sum(rate(vllm:generation_tokens_total[30s]))",
}


def parse_when(s: str) -> float:
    """ISO-8601 (…Z) or epoch seconds -> epoch float (UTC)."""
    try:
        return float(s)
    except ValueError:
        pass
    base = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    return float(base)


def iso_z(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int((epoch % 1) * 1000):03d}Z"


def window_from_steps(path: Path) -> tuple[float, float]:
    ts: list[float] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        t = rec.get("ts")
        if isinstance(t, str) and len(t) >= 19:
            try:
                ts.append(float(calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))))
            except ValueError:
                continue
    if not ts:
        raise SystemExit(f"no timestamps in {path}")
    return min(ts), max(ts)


def query_range(prom_url: str, query: str, start: float, end: float, step: float, timeout: float) -> dict[float, float]:
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    url = f"{prom_url.rstrip('/')}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("status") != "success":
        raise SystemExit(f"prometheus error for {query!r}: {payload.get('error')}")
    result = payload["data"]["result"]
    out: dict[float, float] = {}
    # aggregated queries return at most one series; merge defensively if more
    for series in result:
        for ts, val in series.get("values", []):
            try:
                out[float(ts)] = float(val)
            except (TypeError, ValueError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prom-url", default="http://localhost:9090")
    ap.add_argument("--steps", type=Path, help="staircase-steps.jsonl to derive the window")
    ap.add_argument("--start", help="ISO-8601 or epoch; overrides --steps")
    ap.add_argument("--end", help="ISO-8601 or epoch; overrides --steps")
    ap.add_argument("--pad", type=float, default=30.0, help="seconds to pad each side of the steps window")
    ap.add_argument("--step", type=float, default=2.0, help="query resolution seconds (default 2)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--out", type=Path, default=Path("server-samples.jsonl"))
    args = ap.parse_args()

    if args.start and args.end:
        start, end = parse_when(args.start), parse_when(args.end)
    elif args.steps:
        s, e = window_from_steps(args.steps)
        start, end = s - args.pad, e + args.pad
    else:
        raise SystemExit("give --steps or both --start and --end")

    print(f"[prometheus-pull] {args.prom_url} {iso_z(start)} .. {iso_z(end)} step={args.step}s")
    series = {label: query_range(args.prom_url, q, start, end, args.step, args.timeout)
              for label, q in QUERIES.items()}
    counts = {k: len(v) for k, v in series.items()}
    print(f"[prometheus-pull] points: {counts}")

    # union of timestamps across queries, sorted
    all_ts = sorted({t for s in series.values() for t in s})
    if not all_ts:
        raise SystemExit("no datapoints returned — check the time window and that the run had load")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        for t in all_ts:
            row: dict = {"ts": iso_z(t)}
            gpu = series["gpu_util_max"].get(t)
            if gpu is not None:
                row["gpu_util_max"] = round(gpu, 1)
            serve: dict = {}
            for k in ("num_requests_running", "num_requests_waiting", "gen_tok_s"):
                v = series[k].get(t)
                if v is not None:
                    serve[k] = round(v, 2)
            if serve:
                row["serve"] = serve
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"[prometheus-pull] wrote {len(all_ts)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
