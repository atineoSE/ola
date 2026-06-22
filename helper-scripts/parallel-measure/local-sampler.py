#!/usr/bin/env python3
"""Zero-dependency local resource sampler for OLA parallel runs.

Runs *inside the sbx sandbox* alongside ``ola``, appending one JSONL line every
``--interval`` seconds. It captures the four local resources the measurement
plan needs to identify the binding constraint at the staircase knee, plus the
heartbeat-freshness discriminator for Q1 (kill vs stall):

    RAM    cgroup memory.current / memory.max / memory.events:oom_kill
    CPU    per-core busy% from /proc/stat deltas  (one core pegged == GIL bound)
    DISK   per-device util% from /proc/diskstats io_ticks deltas
    TREE   summed RSS of the ola process tree (main proc + tool subprocesses)
    BEAT   .ola/heartbeat.json {cap, running, pending} + age of its ts

Everything is ``cat /proc`` + ``cat /sys/fs/cgroup`` — no pip installs, matching
the ola-top zero-dependency rule. Keep the raw JSONL; derive plots offline with
analyze.py.

Usage:
    python3 local-sampler.py --folder /work/agent/01-unit-tests \\
        --out /work/agent/01-unit-tests/.ola/local-samples.jsonl --interval 2
    python3 local-sampler.py --folder ... --once    # one sample to stdout (smoke test)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _utc_now_iso() -> str:
    # UTC, millisecond precision, 'Z' suffix — must match the server sampler so
    # the two streams overlay on one clock (measurement plan: clock-sync).
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"


# ---------------------------------------------------------------- cgroup (RAM) --

def _read_int(path: Path) -> int | None:
    try:
        txt = path.read_text().strip()
        return int(txt) if txt.isdigit() else (0 if txt == "max" else None)
    except (OSError, ValueError):
        return None


def _self_cgroup_v2_dir() -> Path | None:
    """Resolve THIS process's cgroup-v2 dir (e.g. /sys/fs/cgroup/docker/<id>).

    The container is not in a private cgroup namespace, so the v2 *root* has no
    memory.current — the real files live under the per-container subtree named in
    /proc/self/cgroup. Walk up until a dir with memory.current is found.
    """
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            parts = line.split(":")
            if parts[0] == "0":  # v2 unified hierarchy
                cur = Path("/sys/fs/cgroup") / parts[2].lstrip("/")
                while True:
                    if (cur / "memory.current").exists():
                        return cur
                    if cur.parent == cur or str(cur) == "/sys/fs/cgroup":
                        break
                    cur = cur.parent
    except OSError:
        pass
    root = Path("/sys/fs/cgroup")
    return root if (root / "memory.current").exists() else None


def _meminfo() -> dict:
    """/proc/meminfo MemTotal/MemAvailable in bytes — the true no-swap wall."""
    out: dict = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable", "SwapTotal"):
                out[k] = int(v.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def sample_memory() -> dict:
    """Memory: cgroup usage + the real VM wall (MemTotal, no swap) + oom_kill.

    On this sbx micro-VM the cgroup is unlimited (memory.max=max) and the ceiling
    is the VM's physical RAM with SwapTotal=0 — so MemAvailable is the live
    distance to an instant SIGKILL. mem_max falls back to MemTotal when the
    cgroup imposes no limit.
    """
    out: dict = {"cgroup": "v2"}
    d = _self_cgroup_v2_dir()
    if d is not None:
        out["mem_current"] = _read_int(d / "memory.current")
        mx = (d / "memory.max").read_text().strip() if (d / "memory.max").exists() else ""
        out["mem_max"] = None if mx in ("", "max") else int(mx)
        oom_kill = oom = None
        ev = d / "memory.events"
        if ev.exists():
            for line in ev.read_text().splitlines():
                k, _, v = line.partition(" ")
                if k == "oom_kill":
                    oom_kill = int(v)
                elif k == "oom":
                    oom = int(v)
        out["oom_kill"] = oom_kill
        out["oom"] = oom
    mi = _meminfo()
    out["mem_total"] = mi.get("MemTotal")
    out["mem_available"] = mi.get("MemAvailable")
    out["swap_total"] = mi.get("SwapTotal")
    if not out.get("mem_max"):
        out["mem_max"] = mi.get("MemTotal")  # cgroup unlimited -> wall is VM RAM
    return out


# ------------------------------------------------------------- /proc/stat (CPU) --

def read_percore_jiffies() -> dict[str, tuple[int, int]]:
    """Return {cpuN: (busy, total)} jiffies from /proc/stat. busy = total-idle-iowait."""
    out: dict[str, tuple[int, int]] = {}
    try:
        stat = Path("/proc/stat").read_text()
    except OSError:
        return out
    for line in stat.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        name = parts[0]
        if name == "cpu":
            continue  # aggregate line; we want per-core
        nums = [int(x) for x in parts[1:]]
        # user nice system idle iowait irq softirq steal guest guest_nice
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        out[name] = (total - idle, total)
    return out


def percore_busy_pct(prev: dict, cur: dict) -> dict[str, float]:
    pct: dict[str, float] = {}
    for cpu, (busy, total) in cur.items():
        if cpu in prev:
            pb, pt = prev[cpu]
            dt = total - pt
            pct[cpu] = round(100.0 * (busy - pb) / dt, 1) if dt > 0 else 0.0
    return pct


# ---------------------------------------------------- /proc/diskstats (DISK) --

def read_disk_ioticks() -> dict[str, int]:
    """Return {device: io_ticks_ms} for physical devices (skip loop/ram/dm)."""
    out: dict[str, int] = {}
    try:
        lines = Path("/proc/diskstats").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if len(f) < 13:
            continue
        name = f[2]
        if name.startswith(("loop", "ram", "dm-", "fd")):
            continue
        # field layout: major minor name then 11 stats; io_ticks is the 10th stat
        # -> f[2+10] = f[12]
        try:
            out[name] = int(f[12])
        except ValueError:
            continue
    return out


def disk_util_pct(prev: dict, cur: dict, interval_ms: float) -> dict[str, float]:
    util: dict[str, float] = {}
    for dev, ticks in cur.items():
        if dev in prev and interval_ms > 0:
            util[dev] = round(min(100.0, 100.0 * (ticks - prev[dev]) / interval_ms), 1)
    return util


# ------------------------------------------------------ process-tree RSS (TREE) --

def _proc_stat_ppid_comm(pid: int) -> tuple[int, str] | None:
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm is in parens and may contain spaces/parens; split on the last ')'
    rparen = data.rfind(")")
    comm = data[data.find("(") + 1 : rparen]
    rest = data[rparen + 2 :].split()
    ppid = int(rest[1])  # state is rest[0], ppid is rest[1]
    return ppid, comm


def _rss_bytes(pid: int) -> int:
    try:
        statm = Path(f"/proc/{pid}/statm").read_text().split()
        return int(statm[1]) * PAGE_SIZE  # field 2 = resident pages
    except (OSError, ValueError, IndexError):
        return 0


def discover_ola_pid(grep: str) -> int | None:
    """Find the ola scheduler pid by cmdline match; lowest matching pid wins."""
    matches: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if grep in cmd and "local-sampler" not in cmd:
            matches.append(int(entry.name))
    return min(matches) if matches else None


def tree_rss(root_pid: int) -> dict:
    """Sum RSS over root_pid and all its descendants."""
    children: dict[int, list[int]] = {}
    pids: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return {"tree_rss": None, "tree_nproc": None}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        info = _proc_stat_ppid_comm(pid)
        if info is None:
            continue
        ppid, _ = info
        children.setdefault(ppid, []).append(pid)
        pids.append(pid)
    # BFS from root
    seen: set[int] = set()
    stack = [root_pid]
    total = 0
    nproc = 0
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += _rss_bytes(pid)
        nproc += 1
        stack.extend(children.get(pid, []))
    return {"tree_rss": total, "tree_nproc": nproc}


# ------------------------------------------------------------- heartbeat (BEAT) --

def sample_heartbeat(folder: Path) -> dict:
    hb = folder / ".ola" / "heartbeat.json"
    out: dict = {"cap": None, "running": None, "pending": None, "hb_ts": None, "hb_age_s": None}
    try:
        data = json.loads(hb.read_text())
    except (OSError, ValueError):
        return out
    out["cap"] = data.get("cap")
    out["running"] = data.get("running")
    out["pending"] = data.get("pending")
    out["hb_ts"] = data.get("ts")
    ts = data.get("ts")
    if isinstance(ts, str):
        try:
            beat = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            out["hb_age_s"] = round(time.time() - beat, 1)
        except ValueError:
            pass
    return out


# ----------------------------------------------------------------------- main --

def take_sample(folder: Path, ola_pid: int | None, prev_cpu, prev_disk, last_t) -> tuple[dict, dict, dict, float]:
    now = time.time()
    cur_cpu = read_percore_jiffies()
    cur_disk = read_disk_ioticks()
    interval_ms = (now - last_t) * 1000.0 if last_t else 0.0

    row: dict = {"ts": _utc_now_iso()}
    row.update(sample_memory())
    row["percore_cpu"] = percore_busy_pct(prev_cpu, cur_cpu) if prev_cpu else {}
    row["disk_util"] = disk_util_pct(prev_disk, cur_disk, interval_ms) if prev_disk else {}
    try:
        row["loadavg"] = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        row["loadavg"] = None
    if ola_pid is not None and Path(f"/proc/{ola_pid}").exists():
        row.update(tree_rss(ola_pid))
        row["ola_pid"] = ola_pid
    else:
        row["tree_rss"] = None
        row["ola_pid"] = None
    row.update(sample_heartbeat(folder))
    return row, cur_cpu, cur_disk, now


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", required=True, type=Path, help="agent phase folder (holds .ola/)")
    ap.add_argument("--out", type=Path, help="JSONL output path (default: <folder>/.ola/local-samples.jsonl)")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between samples (default 2)")
    ap.add_argument("--pid", type=int, help="ola scheduler pid (default: auto-discover)")
    ap.add_argument("--pid-grep", default="ola", help="cmdline substring to find ola pid (default 'ola')")
    ap.add_argument("--once", action="store_true", help="emit one sample to stdout and exit (smoke test)")
    args = ap.parse_args()

    ola_pid = args.pid or discover_ola_pid(args.pid_grep)
    if ola_pid is None:
        print(f"[local-sampler] WARN: no ola pid found (grep={args.pid_grep!r}); tree_rss will be null",
              file=sys.stderr)

    if args.once:
        prev_cpu = read_percore_jiffies()
        prev_disk = read_disk_ioticks()
        time.sleep(min(1.0, args.interval))
        row, *_ = take_sample(args.folder, ola_pid, prev_cpu, prev_disk, time.time() - 1.0)
        print(json.dumps(row, sort_keys=True))
        return 0

    out_path = args.out or (args.folder / ".ola" / "local-samples.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[local-sampler] folder={args.folder} pid={ola_pid} interval={args.interval}s -> {out_path}",
          file=sys.stderr)

    prev_cpu: dict = {}
    prev_disk: dict = {}
    last_t = 0.0
    with out_path.open("a", buffering=1) as fh:
        while True:
            start = time.time()
            # re-discover pid if the scheduler restarted (pool retire on cap raise)
            if ola_pid is None or not Path(f"/proc/{ola_pid}").exists():
                ola_pid = args.pid or discover_ola_pid(args.pid_grep)
            row, prev_cpu, prev_disk, last_t = take_sample(args.folder, ola_pid, prev_cpu, prev_disk, last_t)
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            sleep = args.interval - (time.time() - start)
            if sleep > 0:
                time.sleep(sleep)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
