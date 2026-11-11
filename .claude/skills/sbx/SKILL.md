---
name: sbx
description: Manage Docker sandbox environments using the sbx CLI
version: 3.0.0
---

# sbx — Docker Sandbox CLI

> **Supported release: sbx `v0.51.0`** (`7d55286de0c775148b985ed349f760f1eba06203`).
> This skill documents that release only. ola keeps no compatibility code or
> notes for other sbx releases; git history has the old ones.
>
> Verified on 2026-11-10/11 from the v0.51.0 `--help` text, the
> [v0.50.0](https://github.com/docker/sbx-releases/releases/tag/v0.50.0) and
> [v0.51.0](https://github.com/docker/sbx-releases/releases/tag/v0.51.0)
> release notes, and live throwaway sandboxes created with ola's exact flags on
> `ola:dev` (built `FROM docker.io/sbx/shell-docker-image`): create exit 0,
> `exec -w` exit 0, nested `dockerd` runs `hello-world`, no `*_API_KEY` in the
> sandbox env, `rm -f` exit 0. A statement marked *not re-verified on v0.51.0*
> was last checked on an older release and should be tested before relying on
> it.
>
> ola's invocations: `--version`, `create shell --name --template --skills off
> -m --no-progress <path>`, `run --name`, `exec [-w]`, `ls`, `policy allow
> network` / `policy ls --type network [--decision allow --wide]`, `template
> ls`.

## Version pinning (how ola ties itself to one sbx release)

- **The pin:** `[tool.ola] sbx-version` in `pyproject.toml` — the only place
  the supported sbx release is written.
- **The check:** `_ola_check_sbx_version` (in `ola.sh`) runs first in
  `_ola_sandbox_prepare`, so it covers `ola-sandbox` and `ola-monitor`. It
  reads `sbx --version` (`sbx version v0.51.0 <sha>`; does not start the
  daemon) and:
  - older than the pin → **fails** with the required version;
  - equal → silent;
  - newer → **warns** and continues (sbx ships about weekly and self-updates;
    blocking every run after an update is worse than an unverified run);
  - either version unreadable → warns and continues.
- **The release notes** of every ola version state "Requires sbx vX.Y.Z" (see
  the `ola-release` skill).

**Moving to a new sbx release** (the only way the pin changes):

1. Read the new release notes (`gh release view vX.Y.Z -R docker/sbx-releases`)
   and the `--help` of every command in the invocation list above.
2. Create a throwaway sandbox with ola's exact flags on `ola:dev` and check:
   create and `exec -w` exit 0, `env | grep _API_KEY` empty, `docker run --rm
   hello-world` works, `SANDBOX=1` reaches `ola` through `env`, `rm -f`
   removes it. Time a first and a second create.
3. Fix ola for anything that broke, then **rewrite this skill to describe the
   new release only** — remove anything that no longer holds and any
   compatibility code for the old release. Bump this skill's `version`.
4. Set `sbx-version` to the new release; run `make test`.

## Quick Reference

### Lifecycle
- `sbx run claude [path]` — start/reconnect a Claude Code sandbox (creates if absent).
- `sbx run --name <SANDBOX>` — reconnect to an existing sandbox. The positional of `run` is the *agent*, not the sandbox; the agent is read from the sandbox's spec.
- `sbx run --rm …` — remove the sandbox when the session ends (not with `-d`). Unused by ola, whose sandboxes persist.
- `sbx run -d`/`--detach` — start the sandbox and print its ID without opening a session. `-w`/`--workdir` starts the session in a directory inside the workspace, including on a re-attach.
- `sbx create shell --name <n> --template <img> [--no-progress] <path> [<extra>[:TARGET][:ro]]` — create without attaching. There is no `-q`; `--no-progress` suppresses progress and still prints `✓ Created sandbox <name>`.
- `sbx ls [--format table|json|yaml] [-q]` — columns `SANDBOX AGENT STATUS PORTS WORKSPACE CREATED`, state marks `● running` / `○ stopped`. `--json` is a bare array (`.[].name`). Unreadable sandbox state is reported, not silently omitted.
- `sbx inspect <name> [--format json|yaml]` — full detail, incl. the image (`sandbox-kit-assembled:<hash>`, see *Custom Templates*), bind mounts, and the sbx version that created it.
- `sbx stop <name>…` — stop, keep state. `sbx exec` on a stopped sandbox starts it.
- `sbx rm [--force] [--all] <name>…` — delete sandbox(es) and all state. `--force` is needed for a running/in-use sandbox and without a TTY.
- `sbx exec [-it] [-u root] [-w DIR] [-e K=V] [--env-file F] <name> CMD [ARG…]` — run a command inside. **Exit status is the command's; 125 if sbx could not run it (e.g. no such sandbox); 127 for a missing command.** `-d` is not supported.
- `sbx cp [-L] SRC DST` — copy host⇄sandbox; the sandbox side is `SANDBOX:PATH`. Symlinks that escape the workspace are blocked.
- `sbx diagnose [--format table|json|yaml|github-issue] [--upload]` — installation/daemon/connectivity checks. Warns when `SSL_CERT_FILE`/`SSL_CERT_DIR` replace the system trust store.
- `sbx prune [--dry-run [--json]] [--force] [--filter until=TIMESTAMP]` — remove **stopped** sandboxes only; safe to run habitually. `until=` takes RFC 3339, a Unix timestamp, or a Go duration relative to now (`until=168h`). Secrets scoped to a pruned sandbox go with it.
- `sbx reset [--force] [--preserve-secrets]` — stop everything and clear all state, secrets and policies; reports how many user-added and approval-granted rules it removes. Run `sbx policy init` afterwards.
- `sbx --version` — client version, without starting the daemon. `sbx version` prints the same.

Global flags: `-D`/`--debug`; `--no-input` (never prompt — a command that needs an answer fails and prints the command to run). Prompts need a TTY or `--force`; a piped `y` confirms nothing. A cancelled create exits 130.

Output: `--format table|json|yaml` everywhere (`--json` is an alias; `--format`/`--json`/`-q` are mutually exclusive), JSON is `snake_case`. Results go to stdout; errors, warnings, hints, daemon-start progress and image pulls go to **stderr**.

### Flags on `create`/`run`

- `-m`/`--memory` — e.g. `8g`. Minimum 512 MiB; default 50% of host memory, clamped to 512 MiB–32 GiB; maximum host memory, **warns above 75%**. See *Resource limits*.
- `--cpus` — 0 = auto: all host CPUs (at most 16 on Linux arm64).
- `-t`/`--template <image>`, `--pull always|missing|never`. A tar loaded with `sbx template load` under an unqualified tag is stored as `docker.io/library/<name>:<tag>` and `--template <name>:<tag>` resolves it with no pull, so `ola.sh`'s `--template ola:dev` needs no `--pull never` despite what `template save --help` suggests.
- `--skills off|readonly|readwrite` — defaults to `readonly`; ola passes `off`. See *Shared skills store*.
- `-e`/`--env KEY=VALUE` (or bare `-e KEY` from the host env), `--env-file FILE` — repeatable; `--env` beats files, a later file beats an earlier one. On `run` they apply to the session (so also to a re-attach); on `create` they are baked in.
- `-p`/`--publish [[HOST_IP:]HOST_PORT:]SANDBOX_PORT[/PROTO]` — at creation; on `run` only when that run creates the sandbox.
- `--deny-network HOST` — per-sandbox deny rule at creation, repeatable. There is no local allow-at-creation flag (`--allow-network` is cloud-only); allow with `sbx policy allow network --sandbox <name> …` after create.
- `--static-mcp a,b` / `--static-mcp none` — fix the MCP server set at creation.
- `--mount HOST:SANDBOX_PATH` — bind a host dir to a path a kit declared as `host-mount@1`; cannot add undeclared targets.
- `--clone` — run on a private in-container clone of the host repo (see *Git Workflow*).
- `--kit <ref>`, `--kit-arg name=value`, `--kit-args-file FILE` (experimental), `--profile <governance-profile>`.
- Extra workspaces: `PATH[:TARGET][:ro]` positionals after the main path; a `:ro` one may be a single file. Targets that would hide the shared skills store are rejected.

Agents: claude, codex, copilot, cursor, devin, docker-agent, droid, gemini, kiro, opencode, shell. Built-in names resolve to published kits at `docker.io/sbx/<name>:latest` (override per agent with `kit.shortcuts.<agent>`). The first positional may instead be a kit reference (directory, ZIP, git repo, OCI); a relative one must be written as a path (`./my-kit`). ola always passes `shell`. Omitting PATH creates a sandbox with no workspace bind mount; ola always passes the project dir.

> **Relevance to ola:** ola injects env through its own `~/.ola/agent.env`
> sidecar (written with `sbx exec` + base64) and sets `SANDBOX=1` with `env`
> on the `ola-monitor` exec line. `-e`/`--env-file` could replace that but
> offer nothing more.

### Cloud mode (`--cloud`) — ola is local-only

`--cloud` dispatches to Docker's hosted Sandboxes API instead of local
`sandboxd`, with verbs of its own: `attach`, `move SANDBOX --to local|cloud`
(filesystem only — processes, memory and secrets do not travel), `ttl`, and
`volume`; `sbx --cloud inspect` shows one cloud sandbox. Cloud sandboxes default
to 2 CPUs / 4 GiB, and a `--template` must already exist in the cloud registry.

**ola never passes `--cloud`.** It matters only because `create`/`run` list
flags that look local and are not: `--allow-network`, `--image-ref`,
`--platform`, `--ttl`, `--on-timeout`, `-v`/`--volume`, and on `run` `--new` and
`--detach-keys`. In particular `--allow-network` is **not** the local
counterpart of `--deny-network`.

### Shared skills store (`sbx skills` + `--skills`)

sbx keeps a host-side skills store (macOS:
`~/Library/Application Support/com.docker.sandboxes/sandboxes/agent-skills`)
and **mounts it into every new sandbox by default** (`readonly`), at the
agent's skills directory (e.g. `~/.claude/skills`). `sbx skills add
<repo|owner/name> [--skill NAME]` installs from git; `sbx skills import` copies
from the host's agent dirs (`~/.agents/skills`, `~/.claude/skills`,
`~/.config/opencode/skills`, `~/.copilot/skills`, `~/.cursor/skills`,
`~/.factory/skills`); `ls`/`rm`/`update` do the obvious. Override per sandbox
with `--skills off|readonly|readwrite`, or globally with `skills.defaultMode`.

**ola passes `--skills off` on `sbx create shell`.** A skill in the host store
would be ambient context for every task agent — undeclared in the agent folder,
absent from the plan and the project repo, and different on the next machine.
The agent folder declares its own environment (`allowlist.txt`,
`provision.sh`, `run-init.sh`), so sandbox tooling arrives through those seams
or not at all. (`readwrite` would be worse: task agents mutating a store shared
by every sandbox on the host.)

### sbx runs its own `apt-get update` at every start *(not re-verified on v0.51.0)*
Every sandbox *start* — including the one an `sbx exec` triggers on a stopped
sandbox — kicks off, in the background:
```
sh -c 'command -v apt-get >/dev/null 2>&1 && (apt-get update -qq -y >/dev/null 2>&1 || true) &'
```
It is detached, with **no readiness signal**, so apt work in the first seconds
of a sandbox's life races it and fails with `Could not get lock
/var/lib/apt/lists/lock`. Pass `apt-get -o DPkg::Lock::Timeout=120` rather than
sleeping or polling. The image has no systemd and no apt periodic timers.

### Stopping a process *inside* a sandbox
A process started with `sbx exec <name> CMD` keeps running after the client
exits — **killing the host-side `sbx exec` (Ctrl-C, a harness `TaskStop`) does
not stop it.** Kill it inside:
```
sbx exec <name> pkill -f bin/ola        # -9 if it ignores SIGTERM
```
Relaunching ola without this leaves orphaned `ola`/agent processes that pile up
and can wedge the micro-VM (recover with `sbx rm --force <name>`). `sbx stop
<name>` stops every process in the sandbox.

### Resource limits & swap

A sandbox is its **own micro-VM**: `/proc/meminfo` `MemTotal` equals its `-m`,
not the Docker Desktop VM total. The only resource knobs are `-m` and `--cpus`;
there is no swap flag and no global swap setting.

- **The `-m` default is half of host memory (≤32 GiB), silently.** Set it
  explicitly. ola sets 80% of the Docker VM, clamped to 32 GiB and to 75% of
  host memory (`_ola_sbx_memory`); `OLA_SBX_MEMORY` overrides that, and a value
  above 75% of host memory is accepted with a warning.
- **There is no swap, and you cannot add it** *(not re-verified on v0.51.0)*.
  `SwapTotal: 0`; `swapon` of a swapfile fails with `EINVAL` on the overlay
  rootfs and no block device is exposed for a swap partition. Docker Desktop's
  VM swap setting does not reach sbx micro-VMs, and a template cannot fix it.
- **So the sandbox is a hard RAM wall.** Crossing `-m` is an **immediate OOM
  kill** (SIGKILL) — an abrupt, silent process death, not a slowdown. Keep
  *peak* usage well under `-m`: for ola, `concurrency × peak_RAM_per_agent`
  (plus their containers, below) must sit comfortably below it.

### Docker *inside* a sandbox

ola's image is `FROM docker.io/sbx/shell-docker-image`, which ships a **nested
`dockerd`**, already running before the first command. The `agent` user is in
the `docker` group, so `docker` needs no sudo and `provision.sh` has nothing to
install. Observed on v0.51.0: `dockerd 29.9.3`, storage driver `overlayfs`,
`docker run --rm hello-world` works.

- **It is a nested daemon, not the host socket.** `docker ps` inside lists none
  of the host's containers, and an image built on the host is invisible — every
  image is pulled or built inside.
- **The image store has its own disk cap** *(not re-verified on v0.51.0)*:
  `/var/lib/docker` is a separate volume, **10 GiB by default**, sized by
  `sandbox.disk.dockerVolume` at create time only (existing sandboxes are never
  resized; `DOCKER_SANDBOXES_DOCKER_SIZE` overrides it for one sandbox).
- **Pulls go through the ordinary policy proxy.** The default profile allows
  `docker.io`, `ghcr.io`, `quay.io`, `gcr.io` and `registry.k8s.io`; a private
  registry needs an allow rule.
- **Containers spend the sandbox's `-m`**, under the same no-swap wall.
- **A container outlives the task that started it** — it is a child of the
  sandbox's `dockerd`, so killing the agent, ola or the `sbx exec` does not stop
  it, and the image store only grows. Reclaim both at run boundaries from the
  agent folder's `run-init.sh` (label containers and reap by label; `docker
  image prune -f`) — see the `ola-plan` skill.

### Network Policies
**Global is the default scope.** Pass RESOURCES bare for a global rule; scope to
one sandbox with the `--sandbox <name>` flag (not a positional). Do not pass the
deprecated `-g`/`--global`: it prints a deprecation notice to stderr, which
pollutes captured error output. An empty `--sandbox` value is rejected.

- `sbx policy init <allow-all|balanced|deny-all>` — set the global baseline, once, before adding rules.
- `sbx policy ls [SANDBOX] [--type network|http|filesystem] [-q]` — a one-line summary per policy (`network: 347 allow`). Add **`--wide`** for the rule table with rule IDs and resources (RESOURCES is the last column, one resource per continuation line; `ola-policy-review` parses it), `--decision`/`--source`/`--protocol`/`--created-via` to filter, `--include-inactive` for rules remote governance made inactive. The implicit default-deny fallback is listed separately from explicit denies.
- `sbx policy allow network "domain1,*.domain2"` — global allow; `--sandbox <SANDBOX>` scopes it. Re-adding a covered resource exits 0 (`Already covered: …`).
- `sbx policy deny network "domain"` — deny always beats allow; an explicit CIDR deny beats a matching domain allow.
- `sbx policy rm network --resource "domain"` or `--id <RULE_ID>` (the RULE_ID column of `--wide`, not the rule name; a wrong one prints the corrected command), plus `--sandbox` for a scoped rule.
- `--method GET,HEAD [--path '/v1/**'] [--query name=value]` on `policy allow|deny|rm network` — HTTP-level rules (`policy ls --type http`). Unused by ola.
- `sbx policy check network <host|URL> [--protocol tcp|udp]` / `sbx policy check http` / `sbx policy inspect <policy-or-rule>`.
- `sbx policy approval ls | inspect <ID> | respond <ID> --option allow|dismiss` — the queue of refused requests. A request with no matching rule is **still denied at once**; the approval is a record waiting for an answer, and `allow` writes a persistent rule (`--created-via approval`). **ola never answers these** — task egress belongs in `allowlist.txt`, where it is declared and reviewed. The queue is a good list of what a run tried to reach and was refused.
- `sbx policy log [SANDBOX] [--type network] [--limit N] [--json]` — allowed/blocked requests (`{updated_at, allowed, blocked}` in JSON).
- `sbx policy refresh`, `sbx policy reset`, `sbx policy profile …`.

#### The RESOURCES grammar

A comma-separated list. **Anything outside these forms is rejected**, not
stored as a rule that matches nothing — and **ola aborts the whole sandbox
prepare on a rejected rule**, which is why `_ola_allow_host` adds the `*.<host>`
companion only to a plain domain and passes everything else through untouched.

| Form | Example |
|------|---------|
| exact domain | `example.com` |
| wildcard subdomain (one label) | `*.example.com` |
| multi-label wildcard | `**.example.com` |
| single-character glob | `api?.example.com` |
| character class | `api[12].example.com`, `api[!1].example.com` |
| port suffix | `example.com:443` |
| bare IPv4 | `10.0.0.5` — never `*.10.0.0.5` |
| CIDR prefix | `10.0.0.0/24`, `2001:db8::1/128` |
| IPv6 | only bracketed with a port (`[2001:db8::1]:443`) or as CIDR — a bare IPv6 address is refused |
| all hosts | `**` |

A bare `*` and escaped globs (`\*`) are rejected. Rules are **TCP-only by
default**; `--protocol tcp|udp` (or `tcp,udp`) selects the transport. ola never
passes `--protocol`.

#### Non-HTTP TCP egress (databases) *(not re-verified on v0.51.0)*
A sandbox is not limited to HTTP: non-HTTP TCP (SSH, database wire protocols)
is allowed by a rule for the destination. `allowlist.txt` → `sbx policy allow
network` already produces the right shape.

- **A bare-hostname allow rule does double duty**: it unblocks the DNS lookup
  for that host *and* the raw TCP connect on any port (e.g. Mongo's 27017).
- **Do not use `host:port` or `IP:port` for a TLS service that sends SNI** — the
  proxy matches the SNI hostname, so such a rule never applies. `IP:port` is
  only for non-SNI services. An IP rule does not open a *denied* hostname that
  resolves to that IP.
- **UDP is an open question.** `--protocol udp` exists, but nobody has
  verified that the proxy forwards ordinary unicast UDP end to end. UDP to
  multicast, link-local, unspecified and broadcast addresses is always
  rejected (`host.docker.internal` exempt); ICMP has no flag and is presumed
  blocked. So connect to MongoDB with a **seedlist** URI
  (`mongodb://h1,h2,h3/?tls=true&authSource=admin…`), not `mongodb+srv://`
  (SRV/TXT lookups need UDP), until a test inside a sandbox shows otherwise.
- Egress leaves via the **host-side** proxy, so host routing (e.g. a VPN
  bypass) still applies. The `mongo-vpn` skill has the MongoDB recipe.

#### Reaching a service on the host *(not re-verified on v0.51.0)*
Connect to `host.docker.internal:<port>` (it resolves in-sandbox), but **allow
`localhost`**, not `host.docker.internal`: the policy engine evaluates the
connection under the resource name `localhost` (`sbx policy allow network
--sandbox <SANDBOX> localhost`). A rule for `host.docker.internal` alone still
403s with `Blocked by network policy: domain localhost:...`.

### Credentials

**A `shell` sandbox gets no provider credentials from sbx** — `env | grep
_API_KEY` is empty — unless the `shell-credentials` mixin is added (ola does
not). Everything ola's agents need is injected by `ola.sh`:

- **Preferred Claude credential: `CLAUDE_CODE_OAUTH_TOKEN`** from `claude setup-token`
  — a one-year token billed to the Claude subscription that can only make model
  requests (no Remote Control, no claude.ai connectors; ola uses neither).
  Claude Code ranks it above any saved login. `_ola_inject_oauth_token` appends
  it to the sandbox's `~/.ola/agent.env` on every create and reconnect,
  **after** the sidecar write (which overwrites that file); unset or empty on
  the host it is a silent no-op, never echoed. Only a current Claude Code TUI
  (`ct`) honours it — older builds stop at the login screen — which is one
  reason `_ola_update_claude` runs on every prepare.
- **Claude subscription (OAuth) fallback**: `ola-sandbox` copies
  `~/.claude/.credentials.json` from the host into the sandbox on create and
  reconnect (`sbx exec` + base64).
- **Claude Code `settings.json` is ola-owned, not copied from the host.**
  `_ola_inject_cc_settings` writes a generated file on every create/reconnect;
  the `cc`/`ct` backends copy it into each per-task `CLAUDE_CONFIG_DIR` on
  **every run** (`_ALWAYS_REFRESH`) — per-task dirs are stable and never
  deleted, so copy-once would pin a task to its first run's settings. That
  function is the single place these keys are declared. Deliberately **no**
  `"sandbox"`: Claude Code's own command sandbox is redundant inside the docker
  one and would confine writes to the worktree cwd, silently blocking the
  ola-blocked marker in the agent folder above it. Past bypass-permissions,
  each key closes a way the session could escape or outlive the task:
  `"disableRemoteControl": true` (an unattended task agent has no operator for
  claude.ai/code, `--rc`, the auto-start or the in-session toggle;
  `remoteControlAtStartup: false` leaves the toggle live, so it is the wrong
  knob); `"disableAgentView": true` for the *detached* background agents
  (`claude agents`, `--bg`, `/background`, the on-demand daemon), which outlive
  the `claude` process ola started; and `"env":
  {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}` for the *in-session*
  background subagents (`Agent(run_in_background: true)`), which it removes
  from the Agent tool's schema and forces synchronous — it disables Bash
  `run_in_background` too, on purpose: a process meant to survive its tool call
  must daemonize explicitly and be reclaimed by `run-init.sh`. Never copy the
  host `settings.json` in: it drags in personal hooks/MCP.
- **The sandbox's Claude Code is updated on every create, reconnect and
  `ola-monitor` relaunch** (`_ola_update_claude`: `claude update` inside, 180s
  timeout, never fatal). Nothing else updates it after the image build; the
  update persists across `stop`/start. `make release-image` builds
  `--no-cache`, so every release bakes the current CLI as the starting point.
- **macOS Keychain shadows the file — host runs only.** Claude Code caches OAuth
  credentials per `CLAUDE_CONFIG_DIR` in the Keychain under
  `Claude Code-credentials-<sha256(dir)[:8]>`, and that entry **outranks** the
  dir's `.credentials.json`. ola's per-task dirs are stable across runs, so a
  run whose token expired mid-flight leaves a dead entry that poisons that task
  permanently: every later run fails in ~40ms with `Failed to authenticate:
  OAuth session expired and could not be refreshed`, `duration_api_ms: 0`.
  Guards: `cc-credentials` sweeps *expired* entries, and the `cc`/`ct` backends
  delete the entry for a task's dir whenever they refresh it. Inside the
  sandbox there is no Keychain, so this only bites `ola --skip-sandbox`, and
  only without `CLAUDE_CODE_OAUTH_TOKEN`.
- **GitHub CLI (`gh`) auth**: on every create and reconnect `ola-sandbox` reads
  the host's `gh auth token`, injects it as `GH_TOKEN` into `~/.ola/agent.env`,
  runs `gh auth setup-git` inside (so plain git-over-HTTPS works), and allows
  `github.com,*.github.com`. Non-fatal without a host `gh` login.
- **Git commit identity**: `_ola_inject_git_identity` copies the first global
  `user.name`/`user.email` from the host (`--get-all | head -1`, `--includes`
  honoured) into the sandbox's global git config. Both or neither; otherwise
  the image default `ola <ola@localhost>` stays. A repo-local `user.*` still
  wins.

`sbx secret` — **ola never calls it**; here so its errors are legible. Service
secrets are held by the sbx proxy (not the agent, not the OS keychain), which
injects them into outbound calls. Global is the default scope; `--sandbox
<name>` scopes one. Do not pass the deprecated `-g`/`--global` or a positional
sandbox.

- `sbx secret set [SERVICE]` (interactive), `echo "$KEY" | sbx secret set <service>`, `--oauth` (openai, global only), `--sandbox <name>`.
- Services: `anthropic, copilot, cursor, devin, droid, github, google, groq, mistral, nebius, openai, openrouter, xai`.
- `sbx secret ls [-g] [--sandbox <name>] [--service <name>]` (`-g` here is a listing filter), `sbx secret rm <service> [--sandbox <name>] [-f]`, `sbx secret import [SERVICE] [--all] [--dry-run] [--force]` (a service with an OAuth token is skipped).
- Dynamic secrets: `--ref` (1Password `op://` or an AWS Secrets Manager ARN), `--command` (stdout of a shell command, run from a fresh temporary directory, so use absolute paths), `--refresh on-demand|<duration>` (default 55m), `--no-verify`.
- `sbx secret set-custom --host <pat> --env <VAR> --value <secret>` (or `--value-stdin`, keeping the value out of argv) — a secret for a service sbx doesn't know; the sandbox sees only a placeholder in `<VAR>` and the proxy swaps in the value for `--host` (repeatable; `*` one label, `**` any). **A custom secret with no destination host substitutes nothing.** Remove with `sbx secret rm --placeholder <value>`.
- **Registry secrets** for private template/kit pulls: `gh auth token | sbx secret set --registry ghcr.io --password-stdin`. Host-only by default; `--all-sandboxes` (or `--sandbox <name>`) also injects it into sandboxes' registry login. `--registry-auth-endpoint URL` trusts a Bearer auth endpoint on a different host. Daemon pulls also use the `sbx login` session.

### `ola-monitor` (host-side launcher-watcher)
`ola-monitor` (in `ola.sh`) wraps `_ola_sandbox_prepare` plus a non-interactive
`sbx exec` of `ola <args>` to keep an unattended run going across the two stops
it cannot clear from inside the sandbox, each signalled by a host-visible marker
under `<agent-folder>/monitor/`:

- **`auth-escalation.json`** (ola exits 40) — re-pull Keychain credentials,
  re-inject, relaunch. Thrash-guarded: repeated re-heals in a short window mean
  `cc-credentials` isn't fixing it (most often a concurrent rotator), so it
  stops and notifies. A dead Keychain token notifies rather than loops. **With
  `CLAUDE_CODE_OAUTH_TOKEN` set this path is not taken:** the token outranks
  every saved login, so the watcher deletes the marker and stops (exit 1) with
  instructions — run `claude setup-token`, update the variable, open a new
  shell, re-run `ola-monitor`.
- **`rate-limit.json`** (ola exits 41) — sleep to the marker's `resets_at`
  (floored at 60s so a stale or missing epoch can't hot-spin), re-pull
  credentials (a five-hour window outlives the OAuth token), relaunch. Not
  thrash-guarded: a plan outliving several windows is the case this exists for.

`_ola_check_token_age` (top of `_ola_sandbox_prepare`) remembers when each
`CLAUDE_CODE_OAUTH_TOKEN` was first seen and warns from 335 days on (louder from
365); the record is `${XDG_CONFIG_HOME:-~/.config}/ola/oauth-tokens`, one line
per token — a truncated SHA-256 fingerprint, never the token. Never fatal.

Its own flags (`--monitor-sandbox`, `--monitor-thrash-max`,
`--monitor-thrash-window`, `-h/--help`) are validated before
`_ola_sandbox_prepare` runs; `--skip-sandbox` is rejected (exit 2). Both markers
are cleared before the launch loop starts, so only one written by *this*
invocation is trusted — a stale auth marker would misdiagnose an unrelated `sbx
exec` failure, and a stale rate-limit marker would sleep towards a long-past
epoch. `sbx exec` never sources `~/.bashrc`, so the exec line sets `SANDBOX=1`
itself. A 125 from `sbx exec` (sbx could not run ola) passes through as an
ordinary non-zero exit. The create-vs-reconnect check anchors on the SANDBOX
column (`grep -qE "^${name}[[:space:]]"`), so a sandbox whose name merely
contains the target is not mistaken for it.

`_ola_sandbox_prepare`/`ola-sandbox` take the **agent folder as an optional
second argument** (default `../agent`); `ola-monitor` passes whatever it
resolved from ola's own `-f`. A **`provision.sh`** there runs inside the sandbox
on every create and reconnect (base64 through `sbx exec`), aborting the prepare
on a non-zero exit; apt-based scripts must handle the boot-time apt lock above.

### Ports
Format: `[[HOST_IP:]HOST_PORT:]SANDBOX_PORT[/PROTOCOL]` (HOST_PORT omitted = ephemeral; loopback by default).
- `sbx ports <name>` — list published ports (`--json`)
- `sbx ports <name> -p 8080:3000` (`--publish`) — forward host→sandbox; a host port already in use names the sandbox port that holds it
- `sbx ports <name> --unpublish 8080:3000`

### Git Workflow
- **Default: bind mode** — the agent edits the bind-mounted host working tree in place. In a new sandbox, Git keeps the host's ignore decisions for paths in a primary workspace mounted at a repo root.
- **`--clone`** (at `create`/`run`) — the agent works on a private in-container clone (host repo mounted read-only); its commits are reachable on the host via the `sandbox-<name>` git remote. There is no `--branch` flag.
- **ola's parallel mode does its own worktree isolation** inside the sandbox (`src/ola/worktree.py`): one `git worktree` per task, cherry-picked back onto the base branch — independent of `--clone`. See `tests/test_sandbox_worktree.bats`.

### Custom Templates
- Base images: **`docker.io/sbx/shell-image`**, or **`docker.io/sbx/shell-docker-image`** with Docker Engine (ola uses the latter). Layout: Ubuntu 26.04, user `agent` with sudo and in the `docker` group, `NPM_CONFIG_PREFIX=/usr/local/share/npm-global`, `BASH_ENV=/etc/sandbox-persistent.sh`, Ubuntu's nodejs 22 / npm 9.2 (ola's Dockerfile replaces them with NodeSource Node 24).
- `sbx create shell --template <img>` composes the published `docker.io/sbx/shell:latest` kit with the template into a local `sandbox-kit-assembled:<hash>` image (shown by `sbx inspect`); the guest rootfs is still the template's. The CLI prints `○ shell — now ships as a kit; resolving docker.io/sbx/shell:latest` to stderr even with `--no-progress`. Measured: **~70s for the first create from a new image, ~4s for the next** (the composed image is cached).
- Templates are pulled from an **OCI registry**, not the local Docker image store; the dev path is `docker save` → `sbx template load`.
- `sbx template ls` prints a locally loaded image fully qualified (`docker.io/library/ola   dev …`); `ola.sh`'s `ola:dev` lookup matches exactly that.
- `sbx template save` / `ls` / `inspect` / `rm` / `load` — snapshot and manage templates.
- Private images: store a registry secret first (see *Credentials*).
- **Kits (experimental):** `--kit <ref>` layers tooling/policy onto a sandbox; installs are restricted to `kit.allowedSources`; logs land in `/var/log/sbx-kits/<kit>/install-<n>.log` inside the sandbox; v1/v2 kits cannot be combined with v3 kits (the built-in agents are v3). ola uses no kits beyond the implicit `shell` one.

### MCP servers and declarative environments (unused by ola)

Documented so an unexpected flag or error is legible, not as a recommendation.

- **`sbx mcp`** — `add` / `auth` / `inspect` / `load` / `ls` / `rm` register MCP servers for sandbox sessions; `load` pushes a registered server into a *running* sandbox. Pairs with `--static-mcp`, fixed at creation.
- **`sbx env`** (experimental) — `create` / `exec` / `plan` / `rm` / `run` drive a sandbox declared in an `sbxenv.yaml` (agent, mixin kits, mounts, env, secrets, `additionalWorkspaces[].target`, `entrypoint`/`cmd`, `sandboxOptions`). A plan/approve model (`sbx env plan` changes nothing; `-f` for a non-TTY). A `lifecycle:` block runs commands **on the host with your privileges**, which is why it re-asks every time and why `--skip-host-commands` exists. Unrelated to the `-e`/`--env` flags.

## Debugging
- `sbx diagnose` — first stop for installation/daemon/connectivity problems.
- `sbx policy log [SANDBOX]` and `sbx policy approval ls` — what the proxy refused.
- `sbx exec -it <name> bash` — inspect a sandbox interactively.
- `~/Library/Application Support/com.docker.sandboxes/sandboxes/sandboxd/daemon.log` (macOS) — the daemon's JSON log; the CLI's one-line error is often all you get otherwise.
- **`failed to run sandbox container` on create** with `erofs apply failed: Could not format the device : [Error 5] Input/output error` in `daemon.log` — seen with the host disk 97% full (14 GiB free); the same create succeeded after freeing space to 22 GiB. Check host free space first. `erofs … mounted` INFO lines in the same log are normal; grep for `erofs apply failed`.
- Clock drift after sleep? `sbx stop <name>` then `sbx run --name <name>`; published ports are restored on restart.
- Daemon: `sbx daemon status` / `start` / `stop` / **`restart`** / `log-level`. A `database already in use` error means another process or a live sandbox holds the daemon DB.
- **`ensure daemon: sandboxd (PID …) remained running but did not respond within 10s` has two causes that look identical** (`sbx daemon status` says `stopped`, every command fails the same way):
  1. **A stale daemon after an sbx upgrade** — the new client cannot talk to the old `sandboxd`. Fix: `sbx daemon restart` (stop an in-flight ola run first; it may drop running sandboxes). `_ola_sandbox_prepare`'s `sbx ls` probe recognises the message and points here rather than at `sbx login`.
  2. **The caller cannot reach the daemon socket** — e.g. an agent harness running commands inside its own OS-level sandbox (macOS Seatbelt) that denies the socket under `~/Library/Application Support/com.docker.sandboxes/`. The tell is a **fresh** PID in the message after a restart, and `sbx daemon restart` failing with `force kill process N: operation not permitted`.
- Corrupted state? `sbx reset` (`--preserve-secrets` keeps stored secrets).
- `Invalid API key` from `cc`? sbx does not inject provider keys into a `shell` sandbox, so check `env | grep _API_KEY` inside before suspecting the token.

## Key Differences from `docker sandbox`
- No manual proxy config needed (auto-configured).
- Claude credentials: `CLAUDE_CODE_OAUTH_TOKEN`, or `~/.claude/.credentials.json` copied in by `ola-sandbox`.
- The `balanced` policy replaces manual `--allow-host` chains.
- Multiple mounts: `sbx run claude ~/a ~/b:ro`.
- Reconnect: `sbx run --name <SANDBOX>`; `sbx run claude --name <n>` creates-or-runs.
