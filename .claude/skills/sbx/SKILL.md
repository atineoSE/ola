---
name: sbx
description: Manage Docker sandbox environments using the sbx CLI
version: 2.3.1
---

# sbx — Docker Sandbox CLI

> **Contract re-verified against sbx CLI `v0.35.0`** (`01e01520456e4126a9653471e7072e4d9b280321`, client).
> The command shapes below were checked against this exact version. sbx makes
> breaking CLI changes between releases — e.g. native git isolation moved from
> `--branch` to `--clone`; `policy rm network` dropped its positional form for
> `--resource`/`--id`; **`sbx run <SANDBOX>` positional re-attach was deprecated
> in v0.33.0 in favour of `sbx run --name <SANDBOX>`**; and **`sbx policy
> set-default` was renamed to `sbx policy init` in v0.34.0**. After upgrading
> sbx, run `sbx version` and re-verify with `sbx <cmd> --help` before trusting
> this doc. A script that discards stderr/exit codes will silently do nothing
> when an arg shape changes.
>
> **Resource limits & swap (below):** the memory default (`50% of host, max
> 32 GiB`) is unchanged through v0.35.0. The **75%-of-host hard ceiling on `-m`**
> and the no-swap hard-wall behavior were confirmed empirically on a 48 GB host
> at v0.33.0 and have no indication of change since — but were NOT re-run at
> v0.35.0.
>
> **Network policy scope (v0.33.0, still current at v0.35.0)** — `policy
> allow`/`deny`/`rm network` default to **global** scope with `--sandbox` for
> single-sandbox scoping; the old mandatory `-g`/`--global` flag is deprecated
> (still works, prints a deprecation notice). See *Network Policies*. (`secret`
> still uses `-g`/positional SANDBOX — that scoping was NOT changed.)

## Quick Reference

### Lifecycle
- `sbx run claude [path]` — start/reconnect Claude Code sandbox (creates if absent)
- `sbx run --name <SANDBOX>` — reconnect to an existing sandbox by name (agent read from its spec). **The positional `sbx run <SANDBOX>` re-attach form was deprecated in v0.33.0** — the positional is now the *agent*, so a bare name re-attach is unreliable; use `--name`.
- `sbx create shell --name <n> --template <img> [-q] <path> [<extra>:ro]` — create without attaching
- `sbx ls [--json] [-q]` — list sandboxes (status, ports, workspace)
- `sbx stop <name> [<name>...]` — pause sandbox(es), keep state
- `sbx rm [--force] [--all] <name>...` — delete sandbox(es) + all state (`--force` required to delete an **active/running** session, and for non-TTY)
- `sbx exec [-it] [-d] [-u root] [-w DIR] <name> CMD [ARG...]` — run a command inside (docker-exec flags)
- `sbx cp SRC DST` — copy files host⇄sandbox; sandbox side is `SANDBOX:PATH` (one side must be a sandbox)
- `sbx diagnose` — diagnose common installation/connectivity issues
- `sbx reset [--force] [--preserve-secrets]` — nuclear: stop all, clear ALL state/secrets/policies

Resource flags on `create`/`run`: `-m`/`--memory` (e.g. `8g`, default 50% host max 32 GiB; hard ceiling 75% of host — above it `create` fails),
`--cpus` (0 = auto: **all** host CPUs — was N-1 before v0.35.0), `--profile <governance-profile>`, `--kit <ref>` (experimental; repeatable — see *Custom Templates*).

### Stopping a process *inside* a sandbox
A process launched with `sbx exec <name> CMD` keeps running in the sandbox after
the launching client/terminal exits — **killing the host-side `sbx exec` (or a
harness `TaskStop`/Ctrl-C) does NOT stop it.** There is no per-command sbx stop.
To actually stop a long-running in-sandbox process, kill it *inside*:
```
sbx exec <name> pkill -f bin/ola        # e.g. stop an ola run; -9 if it ignores SIGTERM
```
This matters for ola: relaunching without first killing the previous in-sandbox
`ola` leaves **orphaned `ola`/agent processes** that pile up and can thrash the
micro-VM until `sbx exec`/`sbx stop` themselves wedge (recover with
`sbx rm --force <name>`, or restart Docker Desktop). `sbx stop <name>` is the
blunt alternative — it tears down the whole sandbox (all processes), not one.

Agents for `create`/`run`: claude, codex, copilot, cursor, docker-agent, droid, gemini, kiro, opencode, shell.

### Resource limits & swap (verified v0.33.0)

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
- **`-m` has a hard ceiling at 75% of host RAM — overshoot fails the create.**
  Setting `-m` above *75% of the host machine's physical RAM* does not clamp; it
  **rejects the command**: `create`/`run` exits non-zero with
  `invalid memory "40g": memory 40g exceeds the maximum of 36GiB (75% of host
  memory)` (observed on a 48 GB Mac → 36 GiB ceiling; `36g` was accepted, `40g`
  rejected). So the *settable* range is `default 50% (≤32 GiB)` up to a hard
  `75% of host`. For ola, `OLA_SBX_MEMORY` is subject to this ceiling — a value
  above it aborts the sandbox creation rather than silently shrinking.
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
**Global is the default scope (v0.33.0); `-g`/`--global` is deprecated.**
Pass RESOURCES bare for a global rule (applies to all sandboxes). Scope to one
sandbox with the `--sandbox <name>` flag — **not** a positional SANDBOX name.
The old `-g`/`--global` flag still works but prints
`Flag --global has been deprecated, global is now the default; omit --global,
or use --sandbox to target a single sandbox` to stderr — drop it, or it
pollutes captured error output and will break when the flag is removed.

> **Contract reversal (v0.29.0–v0.31.x → v0.33.0):** scope used to be
> MANDATORY (the bare form exited non-zero with
> `ERROR: must specify either --global RESOURCES or SANDBOX RESOURCES`). That
> requirement was reversed: bare is now the global default. Any script still
> passing `-g`/`--global` should drop it.

- `sbx policy init <allow-all|balanced|deny-all>` — set the initial global baseline (run BEFORE adding rules / first sandbox). **Renamed from `sbx policy set-default` in v0.34.0** — the old name still works but prints a deprecation notice. One-time: use `sbx policy reset` to start over.
- `sbx policy ls [SANDBOX] [--type network]` — summary of active policies. Add **`--wide`** for the rule-level table with **rule IDs and resources** (needed for `policy rm --id`); `--json` for the raw filtered response; `--source`/`--decision` to filter.
- `sbx policy allow network "domain1,*.domain2"` — global allow rule (bare = global)
- `sbx policy allow network --sandbox <SANDBOX> "domain"` — sandbox-scoped allow rule
- `sbx policy deny network "domain"` — global deny rule (deny always > allow)
- `sbx policy rm network --resource "domain"` — remove a global rule by resource (or `--id <uuid>` from `policy ls --wide`)
- `sbx policy rm network --sandbox <SANDBOX> --resource "domain"` — remove a sandbox-scoped rule
- `sbx policy log [SANDBOX] [--type network] [--limit N] [--json] [-q]` — view allowed/blocked requests
- `sbx policy reset` — reset policies to defaults
- `sbx policy profile ...` — manage reusable policy profiles
- `sbx policy check network <host>` / `sbx policy inspect <policy-or-rule>` — (v0.35.0) test whether a request is allowed / show full detail on a policy or rule

> **Removal changed (v0.31.x):** `policy rm network` no longer accepts a
> positional RESOURCES argument. You MUST identify the rule with `--resource
> <csv>` and/or `--id <uuid>` (find them via `sbx policy ls --wide` — plain
> `sbx policy ls` is a summary as of v0.35.0 and does not show IDs). The old
> `sbx policy rm network -g "domain"` form is no longer valid.

RESOURCES is a comma-separated list of hostnames/domains/IPs. Supports exact
domains (`example.com`), wildcard subdomains (`*.example.com`), optional port
suffix (`example.com:443`), bare IPv4 (`10.0.0.5` — do NOT append `*.<ip>`),
and `**` for all hosts. Re-adding a covered resource is idempotent (exit 0,
`Already covered: …`).

#### Non-HTTP TCP egress (databases: Mongo/Postgres/…) (verified 2026-07-23, v0.35.0)
A sandbox is **not** structurally limited to HTTP. Docker's own doc: *"Non-HTTP
TCP traffic, including SSH, can be allowed by adding a policy rule for the
destination IP and port."* This applies to database wire protocols too —
`ola.sh`'s `allowlist.txt` → `sbx policy allow network` path already produces
exactly the rule shape needed, no ola code change required.

- **A bare-hostname allow rule does double duty**: it unblocks the sandbox's
  DNS `A` lookup for that host *and* permits the raw TCP connect on any port
  (e.g. Mongo's 27017). No `/etc/hosts` pin, no IP tracking needed.
- **Do NOT use a `:port` suffix or an `IP:port` rule for a TLS service that
  sends SNI** — the proxy matches on the SNI *hostname* when present, so an
  `IP:port`/`host:port` rule never applies and the connect is denied. Use the
  bare hostname instead. (`IP:port` is the right tool only for a non-SNI
  service, e.g. the Docker-doc SSH example.)
- **UDP and ICMP can never be unblocked**, so a `mongodb+srv://` URI (which
  needs SRV/TXT DNS over UDP) can never resolve in-sandbox. The application
  must connect with a **seedlist** URI instead
  (`mongodb://h1,h2,h3/?tls=true&authSource=admin…`).
- Sandbox egress still exits via the **host-side sbx proxy**, which follows
  the *host's* routing table — any host-level route requirement (e.g. a VPN
  bypass) still applies.
- See the `mongo-vpn` skill for the concrete MongoDB-over-VPN + sandbox recipe
  (host route + seedlist derivation) — don't duplicate it here.

### Credentials
- **Claude subscription (OAuth)**: `ola-sandbox` copies `~/.claude/.credentials.json` from host into the sandbox at creation/reconnection time (via `sbx exec` + base64). No API key needed.
- **macOS Keychain shadows the file — host runs only.** Claude Code caches OAuth
  credentials *per `CLAUDE_CONFIG_DIR`* in the macOS Keychain under
  `Claude Code-credentials-<sha256(dir)[:8]>`, and that entry **outranks** the
  `.credentials.json` inside that dir. Because ola's per-task config dirs are
  derived from the task id they are stable across runs, so a run whose token
  expired mid-flight leaves a dead entry that poisons that task **permanently**:
  every later run fails locally in ~40ms with `Failed to authenticate: OAuth
  session expired and could not be refreshed` — `duration_api_ms: 0`, no API call
  made — and re-running `cc-credentials` alone cannot fix it, because the file it
  refreshes is never read. Two guards, both automatic: `cc-credentials` sweeps
  *expired* `Claude Code-credentials-*` entries (leaving the default entry and any
  live one alone), and the `cc` backend deletes the entry keyed to a task's config
  dir whenever it refreshes that dir's credentials. **This is host-only** — inside
  the sandbox there is no Keychain, so the injected file is already the sole
  credential source and the failure cannot occur. It bites `ola --skip-sandbox`.
- **GitHub CLI (`gh`) auth**: on every create **and** reconnect, `ola-sandbox` also
  reads the host's `gh auth token` and injects it as `GH_TOKEN` into the sandbox
  sidecar (`~/.ola/agent.env`), then runs `gh auth setup-git` inside the sandbox
  so plain `git`-over-HTTPS (not just `gh`) works — mirroring the Claude
  credentials flow above. It also auto-allows `github.com,*.github.com` egress.
  Non-fatal when the host has no `gh` login (or no `gh` installed): a warning is
  printed and the sandbox still comes up, just without `gh`/git-over-HTTPS auth.
- Service secrets are held by the sbx proxy (NOT the agent / not the OS keychain); the proxy injects them into outbound API calls. Scope `-g`/global or per-sandbox.
- `sbx secret set -g <service>` — store a global secret (interactive prompt)
- `echo "$KEY" | sbx secret set -g <service>` — non-interactive via stdin
- `sbx secret set -g openai --oauth` — start an OAuth flow instead of a key (openai/global only)
- `sbx secret set <sandbox> <service>` — sandbox-scoped secret
- `sbx secret ls [SANDBOX] [-g] [--service <name>]` — list stored secrets
- `sbx secret rm -g <service> [-f]` — remove a global secret
- `sbx secret import` — (v0.35.0) import credentials detected in host env vars into the keychain
- v0.35.0 services: `anthropic, cursor, droid, github, google, groq, mistral, nebius, openai, openrouter, xai` (changed from v0.31.3: **`aws` and `bedrock` removed, `openrouter` added** — re-check with `sbx secret set --help` after upgrades)
- **Registry secrets** (e.g. `ghcr.io`) authenticate private template/kit pulls:
  `gh auth token | sbx secret set --registry ghcr.io --password-stdin` (host-only;
  add `-g` to also write `~/.docker/config.json` into every new sandbox). Remove with
  `sbx secret rm --registry ghcr.io -f`.

### `ola-monitor` (host-side auth launcher-watcher)
`ola-monitor` (in `ola.sh`) wraps `ola-sandbox`'s create/reconnect + credential-inject
path (`_ola_sandbox_prepare`) plus a non-interactive `sbx exec` of `ola <args>` to
keep an ola run authenticated unsupervised: on the host-visible auth-escalation
marker, it re-pulls Keychain credentials, re-injects them into the sandbox, and
relaunches `ola` — with a thrash guard (repeated re-heals in a short window that
cc-credentials isn't fixing — most often a concurrent rotator, but not always) and
a notify-fallback for a dead Keychain token. Because `sbx exec` never sources
`~/.bashrc`, `ola-monitor`'s exec line also re-applies what login shells get for
free: `SANDBOX=1`, and stripping the placeholder provider API keys `sbx` injects
into every sandbox (`sbx secret import`'s services above) via `env -u` — a live
placeholder `ANTHROPIC_API_KEY` makes the `cc` backend fail with `Invalid API key`
regardless of OAuth token freshness, which looks like but isn't the concurrent-
rotator case. Both env fixups read `docker/placeholder-api-keys.txt` /
`_ola_placeholder_keys` so the key list is declared once, not duplicated between
the Dockerfile's `~/.bashrc` and `ola.sh`. Auth-only scope — no progress reporting
of its own. Full contract in CLAUDE.md.

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
- **Kits (experimental):** `--kit <ref>` on `create`/`run` (directory, ZIP, or OCI; repeatable) layers extra tooling/policy onto a sandbox. Since v0.34.0, kit installs are restricted to an allowlist configured via `sbx settings set kit.allowedSources`; private kit artifacts pull via the same registry secrets as templates. ola does not use kits — this is here so an unexpected `--kit`/allowlist error is legible.

## Debugging
- `sbx diagnose` — first stop for installation/daemon/connectivity problems
- `sbx policy log [SANDBOX]` — check what the proxy is blocking
- `sbx exec -it <name> bash` — inspect sandbox state interactively
- Clock drift after sleep? `sbx stop <name>` then `sbx run --name <name>` (reconnect by sandbox name; positional re-attach deprecated in v0.33.0). Published ports are restored on restart as of v0.34.0.
- Daemon issues? `sbx daemon status` / `sbx daemon start|stop` / `sbx daemon log-level` (v0.35.0 top-level command). A `database already in use` error from a policy/create call means another process (or a live sandbox) holds the daemon DB.
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
- Reconnect to an existing sandbox: `sbx run --name <SANDBOX>` (agent read from spec). The positional `sbx run <SANDBOX>` form was deprecated in v0.33.0; `sbx run claude --name <n>` still creates-or-runs.
- `sbx version` reports client+server version (there is no `--version` flag)
