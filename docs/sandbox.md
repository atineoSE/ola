# Docker Sandbox

Although you can run `ola` directly on your host machine, we strongly recommend using sandboxes for agent isolation. Sandboxes offer a structural barrier that prevents the agent from accessing anywhere in the filesystem and connecting to anywhere in the internet.

This repo provides a custom sandbox template and companion scripts to run `ola` inside of a docker sandox, using [`sbx`](https://docs.docker.com/sandbox/) (microVM). This provides several layers of isolation, including a network proxy to control outbound traffic. See [here](https://docs.docker.com/ai/sandboxes/security/isolation/) for more details on isolation proporties of docker sandboxes.

Note the isolation provided by docker sandboxes is much more strict that the Claude Code [sandbox feature](https://code.claude.com/docs/en/sandboxing), which doesn't offer true filesystem or network isolation, especially when combined with `--dangerously-skip-permissions`.

## Prerequisites

* Install sbx, login, and set your default policy. The recommended policy is "balanced", which defaults to deny traffic except for approved service providers and package managers. See [here](https://docs.docker.com/ai/sandboxes/security/policy/#network-policies) for more information about policies.
* Add an `allowlist.txt` file in the agent folder to allow traffic to specific domains. They apply globally to all local sandboxes and include all subdomains.

## Build and push the template image

The template extends `docker/sandbox-templates:shell-docker` (the `-docker` variant bundles Docker Engine, so agents can run containers inside the sandbox) and must be pushed to an OCI registry — sbx normally pulls templates from a registry directly and does not use the local Docker daemon's image store.

```bash
docker build --no-cache -f docker/Dockerfile -t ghcr.io/$(whoami)/ola:latest --push .
```

## Dev flow (local image, no registry push)

When iterating on ola itself, build a local image and load it into sbx's image store, then point `ola-sandbox` at it via `OLA_SBX_IMAGE`.

```bash
make sandbox-dev                                  # builds ola:dev and loads it into sbx
OLA_SBX_IMAGE=ola:dev ola-sandbox my-sandbox      # creates sandbox from local image
```

Sandboxes are ephemeral — to pick up a new build, just `sbx rm -f my-sandbox` and recreate.

## Shell helpers

Symlink `ola.sh` to your home directory and source it from `.zshrc`:

```bash
ln -sf /path/to/ola/ola.sh ~/.ola.sh
```

Add to your `.zshrc`:

```bash
[ -f ~/.ola.sh ] && source ~/.ola.sh
```

This provides **`ola-sandbox`** — creates or reconnects to a Docker sandbox.

## Create a sandbox

From your project repo directory (the code dir, e.g. `dummy-project/dummy-project`):

```bash
ola-sandbox my-sandbox
```

This will:
1. Extract Claude OAuth credentials from macOS Keychain (`cc-credentials`)
2. Resolve & validate `agent/.env` on the host (`ola env`) — **fails fast** if a mandatory `${VAR}` is unset
3. Apply the network policy from `agent/allowlist.txt` **and** the resolved `.env` endpoints (additive to default policy)
4. Create a sandbox with the workspace root (parent of the project repo) as workspace — both the project repo and `agent/` are writable, sized to **80% of the Docker VM** (see [Sandbox memory](#sandbox-memory) below)
5. Copy credentials into the sandbox, write the resolved env snapshot to `~/.ola/agent.env`, and set the shell to land in the project repo

> Claude Code config: ola injects its own **minimal** `~/.claude/settings.json`
> (`bypassPermissions` + `skipDangerousModePermissionPrompt`, nothing else) — it
> does **not** copy the host's. The docker sandbox is the isolation boundary, so
> Claude Code's own command sandbox would be redundant; worse, it confines writes
> to the worktree cwd and silently blocks cross-worktree writes such as the
> `ola-blocked` marker (which lands in the agent folder, above the worktree).

Running `ola-sandbox my-sandbox` again reconnects to the existing sandbox and re-runs steps 2–3 and the snapshot refresh (picking up changed host values).

Inside the sandbox:

```bash
ola -a cc -l 5
```

## Unattended auth recovery: `ola-monitor`

`ola-sandbox` + `ola -a cc` requires you to notice a loud auth failure (exit
code 40) and manually re-run `ola-sandbox` to refresh credentials. **`ola-monitor`**
automates that: it launches `ola` into the sandbox for you and watches for the
host-visible auth-escalation marker, self-healing (re-pull Keychain creds,
re-inject, relaunch `ola`) without a human in the loop for a one-off credential
rotation. It takes the same arguments as `ola` itself:

```bash
ola-monitor -a cc -f ../agent -l 5
```

The sandbox name isn't part of `ola`'s own arguments, so `ola-monitor` derives it
from the project checkout directory's name; set `OLA_MONITOR_SANDBOX` if the
sandbox you want it to use was created under a different name. If the same
credential breaks repeatedly in a short window (a concurrent `claude` session
sharing the account, which a mechanical re-pull can't win) or the Keychain has no
valid token at all (you've logged out), `ola-monitor` stops and tells you rather
than looping forever. See CLAUDE.md for the full contract.

## Sandbox memory

`sbx` defaults a sandbox to **50% of the Docker VM's memory** (capped at 32 GiB).
`ola-sandbox` overrides this to **80%** at create time, because parallel agent
runs are memory-hungry and the 50% default leaves the box half-idle. The 80% is
computed off `docker info`'s `MemTotal` (the Docker VM size set by the Docker
Desktop *Resources* slider — **not** the Mac's physical RAM) and capped at sbx's
32 GiB ceiling.

This only takes effect on **create**; the limit is fixed for the life of the
sandbox, so to resize, `sbx rm` it and re-create. To set an exact value, export
`OLA_SBX_MEMORY` (any sbx `-m` value, e.g. `OLA_SBX_MEMORY=24g ola-sandbox …`);
the override bypasses the 32 GiB cap.

> **No swap, hard wall.** An sbx sandbox is its own micro-VM with **zero swap**,
> and swap *cannot* be added (the overlay rootfs can't back a swap area; there is
> no sbx flag for it). Crossing the memory limit is an **instant OOM kill**
> (SIGKILL), not a slowdown — which is exactly how a past 20-agent run died
> silently. So size for *peak* usage with real headroom: keep
> `concurrency × peak_RAM_per_agent` comfortably below the limit. The 80% target
> deliberately leaves ~20% for the VM itself. See `.claude/skills/sbx/SKILL.md`
> for the underlying findings.

## Manual usage

If you prefer not to use the helper:

```bash
cd project
# -m mirrors what ola-sandbox sets automatically (80% of the Docker VM here);
# omit it and sbx falls back to its 50% default.
sbx create shell --name my-sandbox --template ghcr.io/$(whoami)/ola:latest -m 12g .
sbx run --name my-sandbox   # re-attach by name (positional re-attach deprecated in sbx v0.33.0)
```

## Network policy

The `balanced` policy provides deny-by-default with allowlists for AI APIs, package managers, code hosts, and registries. To manage policies:

```bash
sbx policy ls --type network          # show active rules
sbx policy allow network "example.com,*.example.com"  # add allow rule
sbx policy log                        # view blocked requests
```

Project-specific domains can be added to `agent/allowlist.txt` (one host per line; subdomains are included automatically). Blank lines, full-line `#` comments, and inline `# ...` comments after a host are ignored. The `ola-sandbox` helper applies these automatically on sandbox creation.

## Laminar tracing (OpenHands only)

Set `LMNR_PROJECT_API_KEY` and `LMNR_BASE_URL` in `.env` to enable trace export to [Laminar](https://www.lmnr.ai) when using the OpenHands agent (`-a oh`). Traces are exported over HTTP (OTLP/HTTP) on the port specified by `LMNR_HTTP_PORT` (default `8000`).

> **Note:** gRPC export (the default in the Laminar SDK) does not work inside Docker sandboxes. The sbx proxy downgrades HTTP/2 to HTTP/1.x, which breaks gRPC. ola uses `force_http=True` to avoid this entirely.

> **Codex tracing is not wired.** The Codex agent (`-a codex`) consumes the `codex exec --json` event stream directly; there is no SDK auto-instrumentation path, so Laminar export is OpenHands-only.
