# parallel-measure

Zero-dependency instruments for measuring an OLA high-concurrency run against a
self-hosted LLM — the concurrency **staircase** plus both-ends resource sampling
that identifies the binding constraint at the knee. Built for the demo described
in `parallel-run-measurement-plan.md`.

Pure stdlib + `cat /proc` + `cat /sys/fs/cgroup` + `nvidia-smi` (ola-top
zero-dependency rule). No pip installs.

| Script | Runs where | Does |
|---|---|---|
| `local-sampler.py` | inside the sbx sandbox | RAM (cgroup + `oom_kill`), per-core CPU (GIL detector), disk util, ola process-tree RSS, heartbeat freshness → JSONL |
| `prometheus-pull.py` | host, via SSH tunnel | **preferred remote half**: range-queries the server's Prometheus (`:9090`, already scrapes vLLM + GPU/DCGM) for GPU%, queue depth, server tok/s → JSONL. Nothing runs on the server; full history. |
| `server-sampler.py` | on the GPU host | fallback remote half: `nvidia-smi` GPU% + mem, vLLM `/metrics` (queue depth, throughput) → JSONL |
| `staircase.py` | on the host driving the run | writes `.ola/concurrency` through N=2…80 with a dwell per step; logs UTC step markers |
| `analyze.py` | offline, after the run | aligns all sources by UTC into per-step windows; prints the staircase table, the knee + saturated resource, the demo-claim check, and the Q1 kill-vs-stall verdict |

See **`RUNBOOK.md`** for the launch order and the host/config prerequisites.

Each script has `--help`; `local-sampler.py` and `server-sampler.py` take
`--once` (single sample to stdout) and `staircase.py` takes `--dry-run` for smoke
tests before a real run.
