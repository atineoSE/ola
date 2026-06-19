---
name: sbx
description: Manage Docker sandbox environments using the sbx CLI
version: 1.1.0
---

# sbx — Docker Sandbox CLI

> **Contract validated against sbx CLI `v0.31.3`** (`8f15ed5dfabbdc512da2bbec59ff723da9390f64`, client).
> The command shapes below were verified against this exact version. sbx makes
> breaking CLI changes between releases (e.g. native git isolation moved from
> `--branch` to `--clone`, and `policy rm network` dropped its positional form
> in favour of `--resource`/`--id`). After upgrading sbx, run `sbx version` and
> re-verify with `sbx <cmd> --help` before trusting this doc. A script that
> discards stderr/exit codes will silently do nothing when an arg shape changes.
>
> **Resource limits & swap (below) re-verified against sbx `v0.32.0`** — the
> memory default and the no-swap hard-wall behavior were confirmed empirically
> on that version (the rest of this doc is still pinned to `v0.31.3`).

## Quick Reference

### Lifecycle
- `sbx run claude [path]` — start/reconnect Claude Code sandbox (creates if absent)
- `sbx run <SANDBOX>` — reconnect to an existing sandbox by name (positional)
- `sbx create shell --name <n> --template <img> [-q] <path> [<extra>:ro]` — create without attaching
- `sbx ls [--json] [-q]` — list sandboxes (status, ports, workspace)
- `sbx stop <name> [<name>...]` — pause sandbox(es), keep state
- `sbx rm [--force] [--all] <name>...` — delete sandbox(es) + all state (`--force` for non-TTY)
- `sbx exec [-it] [-d] [-u root] [-w DIR] <name> CMD [ARG...]` — run a command inside (docker-exec flags)
- `sbx cp SRC DST` — copy files host⇄sandbox; sandbox side is `SANDBOX:PATH` (one side must be a sandbox)
- `sbx diagnose` — diagnose common installation/connectivity issues
- `sbx reset [--force] [--preserve-secrets]` — nuclear: stop all, clear ALL state/secrets/policies

Resource flags on `create`/`run`: `-m`/`--memory` (e.g. `8g`, default 50% host max 32 GiB),
`--cpus` (0 = auto: N-1 host CPUs), `--profile <governance-profile>`.

Agents for `create`/`run`: claude, codex, copilot, cursor, docker-agent, droid, gemini, kiro, opencode, shell.

### Resource limits & swap (verified v0.32.0)

A sandbox is its **own micro-VM**, not a cgroup-limited container sharing the
host Docker VM's memory pool: inside, `/proc/meminfo` `MemTotal` equals the
sandbox's `-m` value (e.g. 8 GiB), **not** the Docker Desktop VM total. So each
sandbox is sized independently, and there are only two resource knobs —
`-m`/`--memory` and `--cpus`. There is **no swap flag and no global config for
one.**

- **The `-m` default is half the VM, silently.** `--memory` defaults to *50% of
  host memory, capped at 32 GiB*. On a 16 GiB Docker Desktop VM that hands the
  sandbox **8 GiB** unless you override it. Nobody picks 8 GiB — it's the
  unconfigured default. Set it explicitly (`sbx run -m 14g …`, leaving headroom
  for the VM itself). The VM ceiling is the Docker Desktop *Resources* slider.
- **There is NO swap, and you cannot add it.** Inside a sandbox
  `/proc/meminfo` shows `SwapTotal: 0`, and swap **cannot be enabled at all**:
  the root filesystem is `overlay`, and `swapon` of a swapfile fails with
  `EINVAL` even though `mkswap` succeeds and the kernel has `CONFIG_SWAP=y` —
  overlayfs (like tmpfs/virtiofs) cannot back a swap area, and no real block
  device is exposed to host a swap partition. The Docker Desktop VM-level *Swap*
  setting does **not** propagate to sbx micro-VMs. A custom template can't fix
  it (the template *is* the overlay rootfs).
- **Consequence: the sandbox is a hard RAM wall with no cushion.** Crossing `-m`
  triggers an **immediate OOM kill** (SIGKILL) — no thrash-and-recover grace
  period, so an overshoot looks like an abrupt, silent process death rather than
  a slowdown. Size for it: keep *peak* usage **well under** `-m`, not just under
  it (`target_workload_peak ≪ -m`), because nothing absorbs a spike. For ola's
  parallel runs this is the binding constraint — `concurrency × peak_RAM_per_agent`
  must sit comfortably below `-m`.

### Network Policies
**Scope is mandatory for allow/deny:** every `policy allow network` and
`policy deny network` call MUST pass `-g`/`--global` (apply to all sandboxes)
or a `SANDBOX` name before RESOURCES. The bare form
(`sbx policy allow network "host"`) exits non-zero with
`ERROR: must specify either --global RESOURCES or SANDBOX RESOURCES`.

- `sbx policy set-default <allow-all|balanced|deny-all>` — set baseline (run BEFORE adding rules / first sandbox)
- `sbx policy ls [SANDBOX] [--type network]` — show active rules (provenance, scope, decision, resources, IDs)
- `sbx policy allow network -g "domain1,*.domain2"` — global allow rule
- `sbx policy allow network <SANDBOX> "domain"` — sandbox-scoped allow rule
- `sbx policy deny network -g "domain"` — global deny rule (deny always > allow)
- `sbx policy rm network -g --resource "domain"` — remove a global rule by resource (or `--id <uuid>`)
- `sbx policy rm network <SANDBOX> --resource "domain"` — remove a sandbox-scoped rule
- `sbx policy log [SANDBOX] [--type network] [--limit N] [--json] [-q]` — view allowed/blocked requests
- `sbx policy reset` — reset policies to defaults
- `sbx policy profile ...` — manage reusable policy profiles

> **Removal changed (v0.31.x):** `policy rm network` no longer accepts a
> positional RESOURCES argument. You MUST identify the rule with `--resource
> <csv>` and/or `--id <uuid>` (find them via `sbx policy ls`). The old
> `sbx policy rm network -g "domain"` form is no longer valid.

RESOURCES is a comma-separated list of hostnames/domains/IPs. Supports exact
domains (`example.com`), wildcard subdomains (`*.example.com`), optional port
suffix (`example.com:443`), bare IPv4 (`10.0.0.5` — do NOT append `*.<ip>`),
and `**` for all hosts. Re-adding a covered resource is idempotent (exit 0,
`Already covered: …`).

### Credentials
- **Claude subscription (OAuth)**: `ola-sandbox` copies `~/.claude/.credentials.json` from host into the sandbox at creation/reconnection time (via `sbx exec` + base64). No API key needed.
- Service secrets are held by the sbx proxy (NOT the agent / not the OS keychain); the proxy injects them into outbound API calls. Scope `-g`/global or per-sandbox.
- `sbx secret set -g <service>` — store a global secret (interactive prompt)
- `echo "$KEY" | sbx secret set -g <service>` — non-interactive via stdin
- `sbx secret set -g openai --oauth` — start an OAuth flow instead of a key (openai/global only)
- `sbx secret set <sandbox> <service>` — sandbox-scoped secret
- `sbx secret ls [SANDBOX] [-g] [--service <name>]` — list stored secrets
- `sbx secret rm -g <service> [-f]` — remove a global secret
- v0.31.3 services: `anthropic, aws, bedrock, cursor, droid, github, google, groq, mistral, nebius, openai, xai`
- **Registry secrets** (e.g. `ghcr.io`) authenticate private template/kit pulls:
  `gh auth token | sbx secret set --registry ghcr.io --password-stdin` (host-only;
  add `-g` to also write `~/.docker/config.json` into every new sandbox). Remove with
  `sbx secret rm --registry ghcr.io -f`.

### Ports
Format: `[[HOST_IP:]HOST_PORT:]SANDBOX_PORT[/PROTOCOL]` (HOST_PORT omitted = ephemeral; loopback by default).
- `sbx ports <name>` — list published ports (`--json` for machine output)
- `sbx ports <name> --publish 8080:3000` — forward host:sandbox
- `sbx ports <name> --unpublish 8080:3000` — stop forwarding

### Git Workflow
- **Default: direct/bind mode** — the agent edits the bind-mounted host working tree in place.
- **sbx-native isolation: `--clone`** (set at `create`/`run` time) — runs the agent on a
  private in-container clone of the host repo (mounted read-only) wired via a git-daemon;
  the agent's commits are reachable on the host through the `sandbox-<name>` git remote.
  There is **no `--branch` flag** (it was removed; `--branch auto` no longer exists).
- **ola's parallel mode does its own worktree isolation** inside the sandbox
  (`src/ola/worktree.py`): one `git worktree` per task, committed and cherry-picked
  back onto the base branch. This is independent of sbx's `--clone` and does not use
  the `.sbx/` directory. See `tests/test_sandbox_worktree.bats` for the lifecycle.

### Custom Templates
- Base image: `docker/sandbox-templates:shell` (flexible — supports any toolchain including Claude Code + OpenHands)
- `-docker` variants include Docker Engine
- Build: `docker build -t org/img:tag --push .`
- Use: `sbx run --template docker.io/org/img:tag claude` (or `sbx create shell --template ...`)
- Snapshot a running sandbox into a reusable template: `sbx template save` / `sbx template ls` / `sbx template rm` / `sbx template load`
- Private images: store a registry secret first (see Credentials → Registry secrets)

## Debugging
- `sbx diagnose` — first stop for installation/daemon/connectivity problems
- `sbx policy log [SANDBOX]` — check what the proxy is blocking
- `sbx exec -it <name> bash` — inspect sandbox state interactively
- Clock drift after sleep? `sbx stop <name>` then `sbx run <name>` (reconnect by sandbox name)
- Corrupted state? `sbx reset` (add `--preserve-secrets` to keep stored secrets)
- LLM calls fail with `Invalid port: ':1]'` (litellm/httpx)? sbx **v0.31.0** added a
  bracketed `[::1]` entry to the injected `NO_PROXY` ("Add bracketed [::1] to
  NO_PROXY for IPv6 loopback"). httpx parses each no_proxy entry as a URL and
  rejects the bracketed form, killing every LLM call before egress. Not a policy
  problem — the proxy env is sbx-injected, not in ola's sidecar `agent.env`. ola
  scrubs it at startup (`ola.sandbox.sanitize_proxy_env`, called in `cli.main`);
  check `env | grep -i proxy` inside the sandbox to see the raw value.

## Key Differences from `docker sandbox`
- No manual proxy config needed (auto-configured)
- Claude credentials: `~/.claude/.credentials.json` copied into sandbox by `ola-sandbox` (OAuth token, not API key)
- `balanced` policy replaces manual `--allow-host` chains
- Multiple mounts: `sbx run claude ~/a ~/b:ro`
- Reconnect to an existing sandbox: `sbx run <SANDBOX>` (positional name; `sbx run claude --name <n>` creates-or-runs)
- `sbx version` reports client+server version (there is no `--version` flag)
