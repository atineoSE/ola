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
4. Create a sandbox with the workspace root (parent of the project repo) as workspace — both the project repo and `agent/` are writable
5. Copy credentials into the sandbox, write the resolved env snapshot to `~/.ola/agent.env`, and set the shell to land in the project repo

Running `ola-sandbox my-sandbox` again reconnects to the existing sandbox and re-runs steps 2–3 and the snapshot refresh (picking up changed host values).

Inside the sandbox:

```bash
ola -a cc -l 5
```

## Manual usage

If you prefer not to use the helper:

```bash
cd project
sbx create shell --name my-sandbox --template ghcr.io/$(whoami)/ola:latest .
sbx run my-sandbox
```

## Network policy

The `balanced` policy provides deny-by-default with allowlists for AI APIs, package managers, code hosts, and registries. To manage policies:

```bash
sbx policy ls --type network          # show active rules
sbx policy allow network "example.com,*.example.com"  # add allow rule
sbx policy log                        # view blocked requests
```

Project-specific domains can be added to `agent/allowlist.txt` (one domain per line). The `ola-sandbox` helper applies these automatically on sandbox creation.

## Laminar tracing (OpenHands only)

Set `LMNR_PROJECT_API_KEY` and `LMNR_BASE_URL` in `.env` to enable trace export to [Laminar](https://www.lmnr.ai) when using the OpenHands agent (`-a oh`). Traces are exported over HTTP (OTLP/HTTP) on the port specified by `LMNR_HTTP_PORT` (default `8000`).

> **Note:** gRPC export (the default in the Laminar SDK) does not work inside Docker sandboxes. The sbx proxy downgrades HTTP/2 to HTTP/1.x, which breaks gRPC. ola uses `force_http=True` to avoid this entirely.

> **Codex tracing is not wired.** The Codex agent (`-a codex`) consumes the `codex exec --json` event stream directly; there is no SDK auto-instrumentation path, so Laminar export is OpenHands-only.
