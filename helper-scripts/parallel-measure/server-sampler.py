#!/usr/bin/env python3
"""Zero-dependency GPU-host sampler for the self-hosted LLM (the remote half).

Runs *on the GPU host* (or anywhere that can reach nvidia-smi and the inference
server's /metrics), appending one JSONL line every ``--interval`` seconds on the
**same UTC clock** as local-sampler.py so the two streams overlay. This is the
half we have never recorded — it carries the demo's money shot (Q5): the server
has headroom while the local box saturates.

    GPU     nvidia-smi: utilization.gpu %, memory.used/total MiB  (per GPU)
    SERVE   vLLM (or litellm proxy) /metrics:
              num_requests_running, num_requests_waiting  (queue depth -> Q5)
              generation tokens/s if exposed              (server aggregate tok/s)

Only stdlib + the `nvidia-smi` binary + an HTTP GET. If --metrics-url is omitted
GPU-only sampling still runs; if nvidia-smi is absent GPU fields are null.

Usage:
    python3 server-sampler.py --metrics-url http://localhost:8000/metrics \\
        --out server-samples.jsonl --interval 2
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# vLLM exposes Prometheus metrics; names have shifted across versions, so match
# a family of likely names and take whatever is present.
_METRIC_PATTERNS = {
    "num_requests_running": re.compile(r"^vllm:num_requests_running(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.M),
    "num_requests_waiting": re.compile(r"^vllm:num_requests_waiting(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.M),
    "gpu_cache_usage_perc": re.compile(r"^vllm:gpu_cache_usage_perc(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.M),
    # generation throughput: counter (sum) — analyze.py differences it to tok/s
    "generation_tokens_total": re.compile(r"^vllm:generation_tokens_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.M),
    "prompt_tokens_total": re.compile(r"^vllm:prompt_tokens_total(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.M),
}


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"


def sample_gpu() -> list[dict] | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    gpus: list[dict] = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            gpus.append({
                "idx": int(parts[0]),
                "util_pct": float(parts[1]),
                "mem_used_mib": float(parts[2]),
                "mem_total_mib": float(parts[3]),
            })
        except ValueError:
            continue
    return gpus


def sample_metrics(url: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode(errors="replace")
    except Exception as exc:  # noqa: BLE001 - any network/parse error -> null this tick
        return {"error": str(exc)[:200]}
    out: dict = {}
    for key, pat in _METRIC_PATTERNS.items():
        vals = [float(m) for m in pat.findall(body)]
        if vals:
            # running/waiting are gauges across replicas -> sum; counters -> sum
            out[key] = sum(vals)
    return out or {"error": "no known metrics matched"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics-url", help="inference server /metrics endpoint (vLLM or proxy)")
    ap.add_argument("--out", type=Path, default=Path("server-samples.jsonl"))
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout per scrape")
    ap.add_argument("--once", action="store_true", help="emit one sample to stdout and exit")
    args = ap.parse_args()

    def take() -> dict:
        row: dict = {"ts": _utc_now_iso()}
        row["gpu"] = sample_gpu()
        if row["gpu"]:
            row["gpu_util_max"] = max(g["util_pct"] for g in row["gpu"])
        if args.metrics_url:
            row["serve"] = sample_metrics(args.metrics_url, args.timeout)
        return row

    if args.once:
        print(json.dumps(take(), sort_keys=True))
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[server-sampler] gpu={'yes' if shutil.which('nvidia-smi') else 'NO'} "
          f"metrics={args.metrics_url or 'none'} interval={args.interval}s -> {args.out}", file=sys.stderr)
    with args.out.open("a", buffering=1) as fh:
        while True:
            start = time.time()
            fh.write(json.dumps(take(), sort_keys=True) + "\n")
            sleep = args.interval - (time.time() - start)
            if sleep > 0:
                time.sleep(sleep)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
