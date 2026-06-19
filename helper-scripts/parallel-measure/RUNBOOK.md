# Parallel-run measurement runbook

Operational steps to run the concurrency **staircase** against the self-hosted
LLM on the **48 GB MacBook** with the **`oh` (threads)** backend, capturing both
ends on one UTC clock, and produce the demo deliverable.

Companion docs (the *why*): `parallel-run-analysis.md`,
`parallel-run-sizing.md`, `parallel-run-measurement-plan.md`.
Instruments (the *how*): this directory.

---

## 0. Host + workspace prerequisites (do once)

| Check | Action |
|---|---|
| **Server up** | `curl -sf https://code.adriantineo.com/v1/models` returns 200 (not TLS EOF). |
| **sbx authenticated** | `sbx login` (interactive Docker auth) — required before `ola` can create the sandbox or before `sbx exec` can inject the in-sandbox sampler. |
| **SSH to GPU host** | `ssh -i ~/.ssh/serenity_coding.pem substrate@code.adriantineo.com` works. Server is 8× H100 80GB; Prometheus on loopback `:9090` already scrapes vLLM + DCGM GPU. |
| **Docker VM RAM** | Docker Desktop → Settings → Resources: set the VM to **≥ 44 GiB** so a 40 GiB sandbox can be backed. (`ola` sizes the sandbox at 80% of the VM, capped at 32 GiB, *unless* `OLA_SBX_MEMORY` is set — which bypasses the cap.) |
| **Sandbox RAM** | Export `OLA_SBX_MEMORY=40g` before `ola` so the sandbox gets 40 GiB (room for 80 agents — see math below). |
| **Malformed env** | The workspace env file is named `agent/.env\` (trailing backslash) — `ola` will not find it. Rename: `mv 'agent/.env\' agent/.env` |
| **Clock sync** | NTP-sync the GPU host and the Mac, or note the offset. Both samplers stamp UTC; the contrast is unreadable if the clocks drift. |

**80-agent RAM math (why 40g):** unbounded context ≈ 370 MB/agent (inferred —
Step 1 pins it). `80 × 0.37 + 0.23 floor + ~1.5 reserve ≈ 31.3 GB`. At 40 GiB
that leaves ~22% headroom. **No-swap means an overshoot is an instant SIGKILL,
not a slowdown** — keep the headroom.

---

## 1. Step 0 — baseline N=1 (de-risk before the staircase)

Pin the constants the sizing model only *assumes*, so the staircase to 80
doesn't walk into the RAM wall blind. Run one agent to task completion at
`concurrency=1` with the samplers on, then `analyze.py`. Read off the real
`peak_rss_gb` for one agent → that is `ctx_peak`. If `80 × ctx_peak + 2 GB >
OLA_SBX_MEMORY`, lower `LLM_MAX_INPUT_TOKENS` (114688 → 32768) or cap the top of
the staircase before proceeding.

---

## 2. Launch order (start the clocks before the load)

**(a) Server half — Prometheus over SSH (preferred).** Nothing needs to run on
the GPU host during the run; Prometheus stores the history. Open a tunnel now so
the clock is shared, and pull the window in step 3:
```bash
ssh -fNL 9090:127.0.0.1:9090 -i ~/.ssh/serenity_coding.pem \
    substrate@code.adriantineo.com        # background tunnel to Prometheus
# (fallback, only if Prometheus is unreachable: run server-sampler.py on the
#  GPU host with --metrics-url http://localhost:8000/metrics)
```

**(b) Mac — start the ola run** on the workspace at a low cap:
```bash
export OLA_SBX_MEMORY=40g
cd ~/Downloads/ola-tests/yt-dlp-demo-01
printf 2 > agent/01-unit-tests/.ola/concurrency      # start small; staircase drives it
ola run agent --agent oh                              # (use the repo's actual run invocation)
```

**(c) Inside the sandbox — local sampler.** The sampler must run *inside* the
sbx sandbox so it reads the sandbox's cgroup, not the host. The workspace is
mounted in, so point it at the agent folder; copy the script in if it isn't on a
mounted path:
```bash
sbx exec <sandbox-name> python3 /work/helper-scripts/parallel-measure/local-sampler.py \
  --folder /work/agent/01-unit-tests \
  --out /work/agent/01-unit-tests/.ola/local-samples.jsonl --interval 2
# if ola's pid isn't matched, pass --pid-grep scheduler  (or --pid <pid>)
# smoke test:  sbx exec <name> python3 …/local-sampler.py --folder … --once
```

**(d) Mac — drive the staircase** (writes the live concurrency file the
scheduler re-reads each tick):
```bash
python3 staircase.py \
  --folder ~/Downloads/ola-tests/yt-dlp-demo-01/agent/01-unit-tests \
  --steps 2,4,8,16,24,32,48,64,80 --dwell 300 --settle 30
# ~45 min for 9 steps. Restores the cap to 2 on finish/Ctrl-C.
# preview:  staircase.py --folder … --dry-run
```

---

## 3. Analyze (offline, after the run)

Pull the server half from Prometheus for the exact staircase window (derives the
window from the step markers), then analyze:
```bash
python3 prometheus-pull.py --prom-url http://localhost:9090 \
  --steps ~/Downloads/ola-tests/yt-dlp-demo-01/agent/01-unit-tests/.ola/staircase-steps.jsonl \
  --out server-samples.jsonl

python3 analyze.py \
  --folder ~/Downloads/ola-tests/yt-dlp-demo-01/agent/01-unit-tests \
  --server server-samples.jsonl --json
```
Prints the per-step table, the **knee** (and which resource saturated there), the
**demo-claim check** (server GPU < 80% + queue empty at the local knee), and the
**Q1 verdict** (kill vs stall vs clean).

---

## 4. Failure probe (settle Q1 deliberately)

Separately from the demo staircase: set `LLM_MAX_INPUT_TOKENS=114688` (unbounded,
the incident config), keep both samplers + `analyze.py` armed, and push the cap
*past* `N_max(RAM)`. Optionally arm `py-spy dump` on the ola pid. Outcome:
- `oom_kill` increments in the local samples ⇒ **kill** (the OOM hypothesis confirmed).
- `oom_kill` stays 0 + heartbeat goes stale + threads parked in `recv` ⇒ **stall**.

`analyze.py`'s Q1 verdict reads both signals automatically.

---

## What "good" looks like on stage

At the knee: **server GPU < 100% and queue empty** while a **local** resource
saturates. With `oh` threads the expected local limiter is the **GIL** (one core
at ~100%, others idle) — `analyze.py` labels this explicitly so it reads as a
known in-process ceiling, not a mystery harness stall. Avoid two outcomes: a
**RAM** crash (no-swap SIGKILL — keep headroom) and **completions/min flat while
all hardware is idle** (harness serialization — the first incident restaged).
