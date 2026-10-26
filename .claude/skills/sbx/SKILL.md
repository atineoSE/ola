---
name: sbx
description: Manage Docker sandbox environments using the sbx CLI
version: 2.17.0
---

# sbx — Docker Sandbox CLI

> **Contract re-verified against sbx CLI `v0.49.2`** (`5d40fd43027b33ed1f1a39c14fb07e2503d6998f`,
> client; previously verified at v0.47.0). **This bump broke ola twice and
> needed an ola-side fix — ola now requires sbx >= v0.48.0.** ola's
> invocations are now `create shell --name --template --skills off -m
> --no-progress <path>`, `run --name`, `exec [-w]`, `ls`, `policy allow
> network` / `policy ls --type network [--decision allow --wide]`, `template
> ls`.
>
> **v0.48.0 / v0.49.x pass — read this before the v0.47.0 notes below.**
> v0.48.0 reworked flags and output across the whole CLI; v0.49.0 was
> skipped; v0.49.1 is fixes; v0.49.2 is a security rebuild. Source: the
> [v0.48.0](https://github.com/docker/sbx-releases/releases/tag/v0.48.0),
> [v0.49.1](https://github.com/docker/sbx-releases/releases/tag/v0.49.1) and
> [v0.49.2](https://github.com/docker/sbx-releases/releases/tag/v0.49.2)
> release notes, the `--help` surfaces ola uses, and a live throwaway sandbox
> on `ola:dev` created with ola's fixed flags: create exit 0 (~66s, prints `✓
> Created sandbox <name>` even with `--no-progress`), `exec -w` exit 0,
> sandbox-scoped allow rules for a domain + `*.` companion and a bare IPv4
> accepted, `stop` then `exec` restarts it, `rm -f` removes it.
>
> - **Breaking for ola: `create -q`/`--quiet` is gone** — `Error: unknown
>   shorthand flag: 'q' in -q`, exit 1. Replacement is `--no-progress`
>   (suppresses progress, still prints the creation summary). Every
>   `ola-sandbox`/`ola-monitor` *create* failed on this; a reconnect to an
>   existing sandbox did not. Fixed in `_ola_sandbox_prepare` and both
>   integration bats files. `--no-progress` does not exist before v0.48.0.
> - **Breaking for ola, silently: `template ls` prints a locally loaded image
>   fully qualified** — `docker.io/library/ola   dev …`, not `ola   dev …`. The
>   `ola:dev` rung grepped `^ola[[:space:]]+dev`, never matched, and fell
>   through to the registry image with no error. The grep now accepts an
>   optional `docker.io/library/` prefix; `--template ola:dev` itself still
>   resolves.
> - Other renamed flags, none used by ola: `run --detached` → `--detach`/`-d`
>   (`--detached` is rejected); `logout -y` → `-f`; `env plan|create|run -y` →
>   `-f`; `template save -d` → `--description`; `diagnose -o` → `--format
>   table|json|yaml|github-issue`; `secret ls|rm --env` → `--env-name`; `policy
>   log -q` removed. `settings ls` is the primary spelling (`list` is an alias).
> - **Output.** `--format table|json|yaml` everywhere; `--json` is an alias, and
>   `--format`/`--json`/`-q` are mutually exclusive. JSON is reshaped and
>   `snake_case`: `ls --json` is a bare array (`.[].name`, not
>   `.sandboxes[].name`), `template ls` likewise, `policy log` is `{updated_at,
>   allowed, blocked}`. Human output got state marks and a `CREATED` column —
>   `ls` is now `SANDBOX AGENT STATUS PORTS WORKSPACE CREATED`, with `●
>   running`. The anchored `^<name>[[:space:]]` match still holds (tested).
>   `policy ls --wide` still ends in RESOURCES, one per continuation line, so
>   `ola-policy-review`'s `$NF` parse holds. Results go to stdout; errors,
>   warnings, hints, daemon-start progress and image pulls go to **stderr**.
> - **Non-interactive.** A piped `y` no longer confirms anything; prompts
>   need a TTY or `--force`, and fail with exit 1 without one (ola already
>   passes `--force` to `rm`). Declining exits non-zero. Without a TTY,
>   `create`/`run` fail if the workspace directory does not exist (ola's
>   project dir always does). Read-only and `--force` runs never start an
>   interactive sign-in.
> - **Lifecycle.** An unresponsive guest makes `exec`/`run`/`cp` fail with
>   stop-and-start guidance instead of hanging, and `ls` marks it not
>   responding. Guest setup now runs once per container start, not per `exec`.
>   `--pull` default is now **never** for saved local snapshots, **missing**
>   for v3 kit compositions, otherwise **always**. Sandbox startup waits for
>   the nested Docker instead of launching competing daemons (v0.49.1).
> - **Git ignore (v0.49.1)**: in newly created sandboxes Git keeps the host's
>   ignore decisions for paths already in a primary workspace mounted at a repo
>   root. ola mounts the project dir, so ignored host files stay ignored.
> - **Policy (v0.49.1)**: an explicit CIDR deny now beats a matching domain
>   allow; `policy refresh` applies a governance change at once; `policy ls`,
>   `settings ls` and `policy approval ls` take `-q` (IDs/keys only). New
>   settings `daemon.log.maxSizeMB|maxBackups|maxAgeDays`, `browser.enabled`,
>   `proxy.sshDirect`. Full templates now ship a C/C++ toolchain.
> - **v0.49.2 is a CA-trust rebuild** after the eMudhra CA compromise (Go
>   1.27.3; built-in template images rebuilt with updated CA certificates on
>   2026-10-21). **Updating sbx does not update an existing sandbox or an
>   image built `FROM` the old template.** ola's image is `FROM
>   docker/sandbox-templates:shell-docker`, so any `ola` release built before
>   2026-10-21 (v0.9.0 was built 2026-10-12) still carries the old CA bundle
>   until it is rebuilt and released, and its sandboxes recreated (see
>   [the advisory](https://docs.docker.com/security/security-announcements/#docker-sandboxes-response-to-emudhra-ca-compromise)).
> - Also, none touching ola: kits v3 (capability groups, startup commands on
>   `kit add`, `SBX_AGENT` env), `setup ssh --auto-create` for Codex/Claude
>   Desktop, sandbox-scoped custom secrets overriding global ones, `mcp
>   unload`, `run --variant`, `cp` symlink-escape block, cloud `--ttl`/
>   `--on-timeout restart` on `move`/`env create`.
>
> **v0.47.0 pass.** Source: the
> [v0.47.0 release](https://github.com/docker/sbx-releases/releases/tag/v0.47.0)
> (same text in Docker's sandboxes release notes), every `--help` surface ola
> uses, and a live throwaway sandbox on `ola:0.8.0` created with ola's exact
> flags: create exit 0 in ~4s, `exec -w` exit 0, sandbox-scoped allow rules for
> a domain + `*.` companion and a bare IPv4 accepted, `stop` then `exec`
> restarts it, `rm -f` removes it. `ls` columns unchanged (`SANDBOX AGENT
> STATUS PORTS WORKSPACE`), so `_ola_sandbox_prepare`'s anchored name match
> holds. No stale-daemon wedge: the first command printed `Starting sandboxd
> daemon...`.
>
> - **`sbx run --rm`** removes the sandbox when the session ends (cannot be
>   combined with `-d`; a dropped *cloud* session keeps its sandbox). Do not
>   adopt it in ola — ola's sandboxes persist across runs on purpose.
> - **`sbx run -d` on an existing local sandbox keeps it running after
>   sessions disconnect**, until stopped or removed; kits gain a local-only
>   `com.docker.sandbox/long-running@1` capability. Read together they hint
>   that a local sandbox may otherwise stop when its last session disconnects.
>   **Not verified.** Observed: a sandbox driven only by `sbx exec` stayed
>   `running` after every exec returned. If an `ola-sandbox` detach ever stops
>   the sandbox under a running ola, this is the first suspect.
> - **The proxy rejects OAuth token grants it does not recognise**, and their
>   responses can no longer replace host-managed credentials. ola holds no
>   `anthropic` sbx secret, so there is nothing host-managed to protect, and
>   Claude Code's in-sandbox headless run on `CLAUDE_CODE_OAUTH_TOKEN` worked on
>   v0.47.0. Whether an in-sandbox *refresh* of the injected
>   `.credentials.json` still passes is **not verified** — if `cc`/`ct` fail
>   with 401 right after a token refresh, suspect this.
> - The proxy now errors rather than pass through an Anthropic API-key
>   creation response it cannot mask, and catches provider OAuth tokens on a
>   hostname that differs only in case or a trailing dot. ola uses neither path.
> - Network: UDP to multicast, link-local, unspecified and broadcast addresses
>   is **always** rejected, whatever the policy (`host.docker.internal` is
>   exempt) — see *Non-HTTP TCP egress*. Since v0.46.0 (missed on that pass), an
>   IP allow rule no longer lets raw TCP reach a *denied* hostname that
>   resolves to that IP. TLS handshakes that fill the inspection buffer are
>   rejected; incomplete ones time out after 2 minutes.
> - Missed on the v0.46.0 pass, **breaking there**: commands given to `secret
>   set --command` / `set-custom --command` run from a **fresh temporary
>   directory**, so a relative helper path breaks. `gh auth token` is
>   unaffected. ola never calls `sbx secret`.
> - Also: `ports --publish` collision message and retry, `settings set`
>   validates proxy values, cloud `ttl` reports stopped sandboxes, kit pull
>   limits and GHCR/Hub signing, MCP `offline_access`. None touch ola.
> - Known older bug surfaced by this pass, **not** a v0.47.0 change:
>   `ola-policy-review` greps `sbx policy ls --type network`, which has printed
>   only a per-policy summary since v0.35.0, so it reported every host
>   `[MISSING]`. Fixed: it now reads `policy ls --type network --decision allow
>   --wide` (RESOURCES is the last column, one per line) and matches hosts
>   exactly or by wildcard suffix rather than by substring.
>
> **v0.46.0 pass — read this before the v0.45.0 notes below.** Read off every
> `--help` surface, then confirmed against a live daemon with a throwaway
> sandbox created with ola's exact `create shell` flags on the `ola:0.8.0`
> image (create exit 0 in ~4s, `exec -w` exit 0, sandbox-scoped allow rules
> for a domain, its `*.` companion and a bare IPv4 all accepted, `rm --force`
> also dropped those rules). No stale-daemon wedge on this upgrade: the old
> daemon was not running, and the first command printed `Starting sandboxd
> daemon...` and carried on.
>
> - **Blocked requests now queue a pending approval — `sbx policy approval
>   ls|inspect|respond`.** Tested: a request with no matching allow rule is
>   still **denied at once** (curl got `403` in 0s, no wait), so an unattended
>   ola run is not held up. But each denial also leaves an entry in `sbx policy
>   approval ls` (title `host:port`, detail `no matching allow rule (default
>   deny)`, options `allow` / `dismiss`) that stays there until someone answers
>   it. `respond <ID> --option allow` writes a **persistent** rule, which `policy
>   ls --created-via approval` lists. ola does not answer approvals and must
>   not: a task's egress belongs in `allowlist.txt`, where it is declared and
>   reviewed, not in a rule someone clicked. The queue is a good list of what a
>   run tried to reach and was refused, next to `sbx policy log`.
> - **HTTP rules: `policy allow|deny|rm network --method M[,M] [--path GLOB]`**
>   (`--method ANY` matches all; `--path` defaults to `/**`), with `policy ls
>   --type http`. Unused by ola — its rules stay host-level.
> - **`policy check network --protocol tcp|udp`**, and `policy rm network -f`.
> - **`exec -d` is now explicitly not supported** ("detached exec (-d/--detach)
>   is not supported"); the flag is still listed, marked "(not supported)". ola
>   never passes it. An in-sandbox process that must outlive its `exec` must
>   daemonize itself (see *Stopping a process inside a sandbox*).
> - **`run -d`/`--detached` is now documented** ("start the sandbox and print
>   its ID without opening an agent session"). Before this it parsed but was not
>   in the flag list. ola uses `create` for this.
> - **The nested `dockerd`'s `/var/lib/docker` is its own volume, 10 GiB by
>   default** — setting `sandbox.disk.dockerVolume` (min 512 MiB; read at
>   create time only, existing volumes are never resized;
>   `DOCKER_SANDBOXES_DOCKER_SIZE` overrides it for one sandbox). Observed
>   inside the probe sandbox: `/dev/vdd 9.8G … /var/lib/docker`. See *Docker
>   inside a sandbox*: images and containers there now have a disk cap as well
>   as the `-m` memory cap.
> - **The injected `NO_PROXY` no longer has the bracketed `[::1]`** — observed
>   `localhost,127.0.0.1,::1,gateway.docker.internal`. ola's
>   `sanitize_proxy_env` scrub is now a no-op here, not a fix; keep it, since an
>   older sbx still produces the form httpx rejects.
> - `--cpus` help now says auto is "all host CPUs, at most 16 on Linux arm64";
>   a `:ro` extra workspace may now name **a single file**, so one path can be
>   kept read-only inside a writable workspace; `--name` help now states the rules
>   (two or more characters, starting with a letter or digit, only letters,
>   digits, hyphens and periods; `default` is reserved). `ola-monitor` defaults
>   the name to the project dir's basename; a name with `_` gets `Warning:
>   sandbox names cannot contain underscores` from `create` (whether it then
>   rewrites or rejects the name was not tested), so pass `--monitor-sandbox`
>   for such a project.
> - New settings (`sbx settings list`), none set by ola: `claude.remoteControl`
>   (default `false` — lets Claude Code's `/remote-control` authenticate with
>   its own session token; this is sbx's side, and ola's `settings.json` keeps
>   `disableRemoteControl: true` on the Claude Code side whatever it says),
>   `clipboard.imagePaste`, `model.providers` (inference endpoints for a `sbx
>   run --model … --provider …` that does not appear in `run --help`),
>   `platform.images.useDHI`, `platform.images.registryMirror`, and
>   `proxy`/`no_proxy` with `.sandbox`/`.daemon` variants.
> - `template save --help` no longer tells you to pass `--pull never`; the
>   `template load` example (`sbx run --pull never -t myimage:v1.0 claude`) does
>   now. The v0.45.0 finding below still holds — a locally loaded template does
>   not need it.
> - Re-checked and **unchanged** at v0.46.0: the `secret` service list, the
>   `-m` range text, `--skills` defaulting to `readonly`, the RESOURCES grammar,
>   `prune`/`rm`/`cp`/`ports`/`reset`/`diagnose` flags, and the agent list.
>   `rm`/`prune` `-f` now say it also deletes a sandbox "in use (e.g. an open
>   SSH connection)".
>
> **v0.45.0 pass.** Read off
> `--help` surfaces, then the two load-bearing items were confirmed against a
> live daemon with throwaway sandboxes.
>
> - **`policy allow network` grammar widened and got strict** — the one change
>   that touches ola's own code path. Patterns now also accept CIDR prefixes,
>   multi-label wildcards (`**.example.com`), single-character globs
>   (`api?.example.com`) and character classes (`api[12]`, `api[!1]`); a bare
>   IPv6 address is **refused** (write `[2001:db8::1]:443` or
>   `2001:db8::1/128`); and **a bare `*`, an escaped glob (`\*`), or any other
>   pattern outside these forms is now rejected outright rather than stored as
>   a rule that matches nothing.** ola turns a rejected rule into an aborted
>   sandbox prepare, so this converts a silent no-op into a hard stop — see
>   *Network Policies* for the `*.<ip-literal>` case that reaches ola.
> - **`policy allow network --protocol tcp|udp` exists, and rules are TCP-only
>   by default.** This contradicts what this skill said flatly until now — see
>   the UDP bullet under *Non-HTTP TCP egress*, which is now marked unverified
>   rather than silently rewritten.
> - **`--pull always|missing|never` on `create`/`run`, defaulting to
>   `always`** — never documented by this skill on those verbs, and it looked
>   like a live hazard because `ola.sh` creates with `--template ola:dev`, an
>   unqualified tag that only exists in the local sbx image store. **Tested,
>   and it is not:** a template loaded locally under an unqualified tag
>   (`sbx template load` of a `docker save` tar, stored as
>   `docker.io/library/<name>:<tag>`) creates fine under the default `always`,
>   exit 0, no pull attempted. **No ola change needed** — do not add
>   `--pull never` on the strength of the flag's name. `sbx template save
>   --help` does still tell you to pass `--pull never`, so the advice and the
>   behaviour disagree; the behaviour won.
> - **`sbx daemon restart`** is new, and is the first-line fix for the
>   post-upgrade wedge instead of hunting the PID. Note it needs privileges to
>   SIGKILL the old daemon: from inside a restricted shell it fails with
>   `force kill process N: operation not permitted; daemon left running, not
>   restarted`.
> - Re-checked and **unchanged** at v0.45.0: the `secret` service list, the
>   `-m` range text, `--skills` defaulting to `readonly`.
> - **ola now passes `--skills off` on `sbx create shell`** (verified accepted;
>   create exits 0). See *Shared skills store*.
> - New and **unused by ola**: `create` with no PATH at all (no workspace bind
>   mount); `policy ls --created-via` and `--protocol` filters; `template save
>   --capture-mode`/`--description`; `secret set --show-error`. `--cloud`'s
>   verb set has grown well past the four below — `cp`, `ports`, `stop`, `rm`,
>   `secret`, `policy`, `template`, `mcp`, `env`, `kit`, `setup` all take it
>   now — but ola never passes it.
>
> **The v0.43.0 headline is `--cloud`**: a global flag plus four new verbs
> (`attach`, `move`, `ttl`, `volume`) that run sandboxes on Docker's hosted
> service instead of local `sandboxd`. ola is local-only and unaffected — but
> `create`/`run` now carry a **block of cloud-only flags that read as local**
> (`--allow-network`, `--image-ref`, `--platform`, `--ttl`, `--on-timeout`,
> `-v`/`--volume`, `--new`, `--detach-keys`). See *Cloud mode* below; the one
> that matters is `--allow-network`, because earlier versions of this skill
> said no such flag existed.
>
> **The one new default that reaches into a sandbox ola creates**: `--skills`
> on `create`/`run` defaults to **`readonly`**, bind-mounting a host-side shared
> skills store at the agent's skills directory (e.g. `~/.claude/skills`). See
> *Shared skills store*.
>
> Two corrections to this skill's v0.39.0 pass:
> - `sbx prune --filter` takes **`until=TIMESTAMP`**, not `since=DURATION`
>   (RFC 3339, Unix epoch, or a Go duration relative to now). It also gained
>   `--json`, and `--force` now removes an in-use sandbox as well as skipping
>   the prompt.
> - The "`sbx create shell --help` prints the *root* help" quirk is **not
>   real** — it was a zsh artifact (an unquoted `$var` holding `create shell`
>   is one word in zsh, so sbx saw an unknown command). At v0.43.0 every
>   nested `--help` renders correctly; `sbx help <cmd> <subcmd>` is what does
>   not work. `sbx run -d` still parses while absent from the flag list.
>
> New since v0.39.0 and **unused by ola**: `setup ssh` / SSH-agent forwarding;
> `template inspect`; `cp -L`; `policy ls --profile`; `diagnose -o
> github-issue --upload`; `-p`/`--publish` on `create`/`run` (ports at
> creation, previously `sbx ports` only); `--kit-arg`/`--kit-args-file`; a much
> larger `env` (a plan/approve model, host `lifecycle:` commands, `args:`).
> `rm` and `prune` now also delete secrets scoped to each removed sandbox, and
> `reset` additionally clears the managed SSH config and Gordon's sessions.
> Carried over and still unused: the `kit` family, `login`/`logout`, `mcp`,
> `tui`.

> sbx makes breaking CLI changes between releases — e.g. native git isolation
> moved from `--branch` to `--clone`; `policy rm network` dropped its
> positional form for `--resource`/`--id`; **`sbx run <SANDBOX>` positional
> re-attach was deprecated in v0.33.0 in favour of `sbx run --name
> <SANDBOX>`**; **`sbx policy set-default` was renamed to `sbx policy init`
> in v0.34.0**; and **`secret` scoping went global-by-default in v0.39.0**.
> After upgrading sbx, run `sbx version` and re-verify with `sbx <cmd> --help`
> before trusting this doc. A script that discards stderr/exit codes will
> silently do nothing when an arg shape changes — and a script that *captures*
> stderr will find a deprecation notice mixed into its error text.

> **Resource limits & swap (below):** at v0.43.0 `create --help` now spells the
> whole range out rather than just the default: *"Minimum: 512 MiB. Default:
> 50% of host memory, clamped to 512 MiB–32 GiB. Maximum: max(75% of host
> memory, 512 MiB)"* — so the **75%-of-host ceiling on `-m`**, found
> empirically on a 48 GB host at v0.33.0, is now documented behaviour rather
> than an observation. The no-swap hard wall is still empirical (v0.33.0) and
> has NOT been re-run since.

> **Network policy scope (v0.33.0, still current at v0.43.0)** — `policy
> allow`/`deny`/`rm network` default to **global** scope with `--sandbox` for
> single-sandbox scoping; the old mandatory `-g`/`--global` flag is deprecated
> (still works, prints a deprecation notice). See *Network Policies*. As of
> v0.39.0 `secret` follows the same rule — see *Credentials*.

## Quick Reference

### Lifecycle
- `sbx run claude [path]` — start/reconnect Claude Code sandbox (creates if absent)
- `sbx run --name <SANDBOX>` — reconnect to an existing sandbox by name (agent read from its spec). **The positional `sbx run <SANDBOX>` re-attach form was deprecated in v0.33.0** — the positional is now the *agent*, so a bare name re-attach is unreliable; use `--name`.
- `sbx run --rm ...` (v0.47.0) — remove the sandbox when the session ends; not with `-d`. Unused by ola, whose sandboxes persist.
- `sbx create shell --name <n> --template <img> [--no-progress] <path> [<extra>:ro]` — create without attaching (`-q` was removed in v0.48.0 and is rejected)
- `sbx ls [--format table|json|yaml] [-q]` — list sandboxes (status, ports, workspace, created). `--json` is a bare array since v0.48.0.
- `sbx stop <name> [<name>...]` — pause sandbox(es), keep state
- `sbx rm [--force] [--all] <name>...` — delete sandbox(es) + all state (`--force` required to delete an **active/running** session, and for non-TTY)
- `sbx exec [-it] [-u root] [-w DIR] [-e K=V] [--env-file F] <name> CMD [ARG...]` — run a command inside (docker-exec flags; `-e`/`--env-file` added since v0.39.0). Starts a stopped sandbox first. **`-d` is not supported** (v0.46.0 says so explicitly).
- `sbx cp [-L] SRC DST` — copy files host⇄sandbox; sandbox side is `SANDBOX:PATH` (one side must be a sandbox). `-L`/`--follow-link` follows symlinks in the source.
- `sbx diagnose [--format table|json|yaml|github-issue] [--upload]` (`-o` removed in v0.48.0; without a TTY `--upload` saves but skips the upload and still exits 0) — diagnose common installation/connectivity issues
- `sbx prune [--dry-run [--json]] [--force] [--filter until=TIMESTAMP]` — remove **stopped** sandboxes only; a running one is never touched, so it is safe to run habitually. Confirms unless `--force` (which also removes an in-use sandbox). `--filter until=` takes an RFC 3339 timestamp, a Unix timestamp, or a Go duration relative to now (`until=168h` keeps anything stopped within the last week) — **not** the `since=` this skill claimed at v0.39.0. A sandbox whose stop time the daemon can't report is left alone. Secrets scoped to a pruned sandbox are deleted with it. Use `sbx rm` to remove a specific sandbox regardless of state.
- `sbx reset [--force] [--preserve-secrets]` — nuclear: stop all, clear ALL state/secrets/policies (and, since v0.39.0, the managed SSH config and Gordon's sessions)

Resource flags on `create`/`run`: `-m`/`--memory` (e.g. `8g`; min 512 MiB, default 50% of host clamped to 512 MiB–32 GiB, hard ceiling 75% of host — above it `create` fails),
`--cpus` (0 = auto: **all** host CPUs, at most 16 on Linux arm64 — was N-1 before v0.35.0), `--profile <governance-profile>`, `--kit <ref>` (experimental; repeatable — see *Custom Templates*), `--kit-arg name=value` / `--kit-args-file FILE` (experimental).

Image flags on `create`/`run`: `-t`/`--template <image>`, and **`--pull
always|missing|never`, which defaults to `always`**. `sbx template save --help`
says to pass `--pull never` "to use the saved image without trying to pull it
from a registry", which reads like a locally-loaded template needs it —
**tested at v0.45.0, it does not**. A tar loaded with `sbx template load` under
an unqualified tag lands in the store as `docker.io/library/<name>:<tag>` and
`sbx create shell --template <name>:<tag>` succeeds under the default `always`,
exit 0, with no pull attempted. So `ola.sh`'s `ola:dev` rung (`make
sandbox-dev` → `docker save` → `sbx template load`, then `create --template
ola:dev` with no `--pull`) is correct as written, and `--pull never` would be
cargo cult. Kept here because the help text implies the opposite.

Env, port and policy flags on `create`/`run` (**none used by ola today**):
- `-e`/`--env KEY=VALUE` or bare `-e KEY` (takes the value from the host environment); repeatable.
- `--env-file FILE`; repeatable. `--env` beats any file; a later file beats an earlier one.
- On **`run`** both apply to the *agent session*, so they take effect on a re-attach too, and are baked into the sandbox when that run creates it. On **`create`** they are baked in at creation.
- `-p`/`--publish [[HOST_IP:]HOST_PORT:]SANDBOX_PORT[/PROTO]` — publish a port at creation time; repeatable. On `run` it applies only when that run *creates* the sandbox and is ignored on re-attach (use `sbx ports` then).
- `--deny-network HOST` — per-sandbox deny rule at creation time; repeatable. Later visible via `sbx policy ls <NAME>` and removable with `sbx policy rm network --sandbox <NAME> --resource <HOST>`. A local deny can only narrow egress, so it is safe under centralized governance.
- `--static-mcp a,b` — fix the sandbox's MCP server set at creation; cannot be changed on re-attach. See `sbx mcp` under *MCP servers*.
- `--skills off|readonly|readwrite` — **defaults to `readonly`**; see *Shared skills store*.

> **Relevance to ola:** ola injects env through its own `~/.ola/agent.env` sidecar
> (written with `sbx exec` + base64) and strips placeholder provider keys with
> `env -u` on the `ola-monitor` exec line. `-e`/`--env-file` — now on `exec`
> too, not just `create`/`run` — could in principle replace part of that, but
> they can only *set* variables; there is still no "unset" form, so the
> `env -u` strip has no sbx-native equivalent. Treat this as a known
> alternative, not a pending migration.

### Cloud mode (`--cloud`, v0.43.0) — ola is local-only

`--cloud` dispatches to Docker's hosted Sandboxes API instead of local
`sandboxd`, and brings four verbs that exist only there: `attach` (PTY into a
running cloud sandbox), `move SANDBOX --to local|cloud` (filesystem-only
transfer via a transport template; processes and memory do not travel, and
secrets never follow), `ttl [+DURATION] SANDBOX` (inspect/extend against a hard
24h-from-creation ceiling), and `volume` (snapshot-on-exit persistent storage;
concurrent mounts are last-writer-wins). Cloud sandboxes default to 2 CPUs and
4 GiB, and `-t`/`--template` must already exist in the cloud registry.

**ola never passes `--cloud`, so none of this is in its path.** It is here for
one reason: `create`/`run` now advertise flags that look local and are not.
`--allow-network`, `--image-ref`, `--platform`, `--ttl`, `--on-timeout`,
`-v`/`--volume` are **cloud-only**, as are `--new` and `--detach-keys` on
`run`. In particular **`--allow-network` now exists** — earlier versions of
this skill said it didn't — but it is a *cloud egress* flag and is **not** the
local counterpart to `--deny-network`. Locally there is still no allow-at-
creation flag; use `sbx policy allow network --sandbox <name> …` after create,
which is what `ola.sh` already does.

### Shared skills store (`sbx skills` + `--skills`, experimental)

sbx keeps a host-side skills store (on macOS,
`~/Library/Application Support/com.docker.sandboxes/sandboxes/agent-skills`)
and **mounts it read-only into every new sandbox by default**, at the agent's
skills directory (e.g. `~/.claude/skills`). `sbx skills add <repo|owner/name>
[--skill NAME]` installs from git, `sbx skills import` copies from the host's
own agent dirs (checked in order: `~/.agents/skills`, `~/.claude/skills`,
`~/.config/opencode/skills`, `~/.copilot/skills`, `~/.cursor/skills`,
`~/.factory/skills`), `ls`/`rm`/`update` do the obvious. Override per sandbox
with `--skills off|readonly|readwrite` at create time, or globally with the
`skills.defaultMode` setting.

**ola opts out: `ola.sh` passes `--skills off` on `sbx create shell`.** This
was the first sbx default that writes into the sandbox's `$HOME` without ola
asking, and `~/.claude` is a directory ola populates itself
(`_ola_inject_cc_settings` writes `$HOME/.claude/settings.json`; the `cc`/`ct`
backends build per-task `CLAUDE_CONFIG_DIR`s *elsewhere*, under the agent
folder). The reason to opt out is not the collision, though — it is that a
skill in the host store becomes **ambient context for every task agent ola
runs**: undeclared in the agent folder, absent from the plan and the project
repo, and different on the next machine. ola's contract is that the agent
folder declares its own environment (`allowlist.txt`, `provision.sh`,
`run-init.sh`), so sandbox tooling arrives through those seams or not at all.
(`--skills readwrite` would be worse still: a task agent mutating a store
shared with every other sandbox on the host.)

The flag is **verified accepted at v0.45.0** — `sbx create shell … --skills
off` exits 0 — and requires sbx >= v0.43.0. It is inert today either way: on a
host that never ran `skills add`/`import` the store is empty, and
`mount | grep -i skill` inside a freshly created sandbox showed **no skills
mount at all**, with or without the flag (the stock `shell` agent may simply
have no skills directory). So this locks in determinism ahead of the default
mattering, rather than fixing something that currently bites.

### sbx runs its own `apt-get update` at every start (verified 2026-08-24, v0.37.1)
Every sandbox *start* — including the one an `sbx exec` triggers on a stopped
sandbox — kicks off, in the background:
```
sh -c 'command -v apt-get >/dev/null 2>&1 && (apt-get update -qq -y >/dev/null 2>&1 || true) &'
```
It is detached and fire-and-forget, with **no readiness signal**, so anything
apt-based you run in the first seconds of a sandbox's life races it and fails
with `Could not get lock /var/lib/apt/lists/lock. It is held by process <pid>
(apt-get)`. Ordering your own work "after setup" does not help — this *is* the
image's boot. Pass `apt-get -o DPkg::Lock::Timeout=120` (apt's own wait) rather
than sleeping or polling. Note the image has no systemd and no apt periodic
timers (`docker-disable-periodic-update` is in `apt.conf.d`) — this one job is
the whole story.

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

Agents for `create`/`run` (unchanged v0.43.0 → v0.45.0): claude, codex, copilot, cursor, **devin**, docker-agent, droid, gemini, kiro, opencode, shell. The first positional may also be a *kit reference* (directory, ZIP, git repo, or OCI) instead of a built-in agent name — a relative one must be written explicitly (`./my-kit`), since a bare word keeps its agent/sandbox-name meaning. ola always passes `shell`. As of v0.45.0 the PATH may be omitted entirely, creating a sandbox with **no workspace bind mount** (the agent works in the container's own filesystem); ola always passes the project dir.

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

### Docker *inside* a sandbox (verified 2026-09-08, sbx v0.39.0, ola image v0.6.5)

A sandbox can run containers itself. The stock `shell` agent template — and
therefore ola's image, which is `FROM docker/sandbox-templates:shell-docker` —
ships a **nested `dockerd`**, already running before the first command
(observed: PID 16, `ppid` 1, root). The `agent` user is in the `docker` group,
so `docker` needs no sudo, and `provision.sh` has nothing to install.

Observed inside a live ola sandbox: `/usr/bin/docker`, client **and** server
`29.7.2`, storage driver `overlayfs`, cgroup v2, `Docker Root Dir:
/var/lib/docker`, socket `/var/run/docker.sock` (`root:docker`, `srw-rw----`).

- **The image store has a disk cap (seen at v0.46.0).** `/var/lib/docker` is
  its own block-device volume, **10 GiB by default** (`/dev/vdd 9.8G`), sized
  by the `sandbox.disk.dockerVolume` setting at create time only. An existing
  sandbox is never resized, so to raise it: change the setting, then `sbx rm`
  and re-create. `DOCKER_SANDBOXES_DOCKER_SIZE` overrides it for one sandbox.
  A task that builds or pulls large images fills this before it fills `-m`,
  and the image store only grows across runs (below) — a further reason for
  `docker image prune` in `run-init.sh`.

- **It is a nested daemon, not the host socket mounted in.** `docker ps` inside
  lists none of the host's containers — including the sandboxes themselves,
  which is the tell — and the image store holds only what was pulled or built
  in there. Consequence: **an image built on the host is invisible**; every
  image is pulled or built inside, and the first pull is paid inside.
- **Pulls go out through the ordinary policy proxy**, so registries obey the
  same rules as any other egress. The default profile already allows
  `docker.io`, `ghcr.io`, `quay.io`, `gcr.io` and `registry.k8s.io` (`sbx
  policy check network registry-1.docker.io` → `Allowed`; a live
  `docker pull alpine:3.20` succeeded with no sandbox-specific rule). A
  **private registry needs an allow rule** like anything else.
- **Containers spend the sandbox's `-m`, not the host's** — same budget, same
  no-swap hard wall as the section above. `concurrency × peak_RAM_per_agent`
  becomes `concurrency × (agent + its containers)`.
- **A container outlives the task that started it.** It is a child of the
  sandbox's `dockerd`, not of the process that ran `docker run`, so killing the
  agent, ola, or the `sbx exec` does not stop it, and neither does deleting the
  worktree — the same trap as a daemonized server, one level down. The image
  store likewise persists across runs and only grows. Reclaim both at run
  boundaries from the agent folder's `run-init.sh` (label containers at
  `docker run` and reap by label; `docker image prune -f` if tasks build
  images) — see the `ola-plan` skill, which carries the planning-side rules.

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
- `sbx policy ls [SANDBOX] [--type network]` — summary of active policies. Add **`--wide`** for the rule-level table with **rule IDs and resources** (needed for `policy rm --id`); `--json` for the raw filtered response; `--source`/`--decision` to filter. `--type` also accepts `filesystem` (v0.37.1); `--source` also accepts `kit`. `--include-inactive` (v0.37.1) shows rules an org's remote governance has made inactive, hidden by default — irrelevant unless remote governance is in play.
- `sbx policy allow network "domain1,*.domain2"` — global allow rule (bare = global)
- `sbx policy allow network --sandbox <SANDBOX> "domain"` — sandbox-scoped allow rule
- `sbx policy deny network "domain"` — global deny rule (deny always > allow)
- `sbx create|run --deny-network <host>` — (verified v0.43.0) add the per-sandbox deny rule **at creation time**, repeatable, instead of a follow-up `policy deny` call. Equivalent to a `--sandbox`-scoped deny: list it with `sbx policy ls <NAME>`, drop it with `sbx policy rm network --sandbox <NAME> --resource <HOST>`. There is **no local `--allow-network`**: the flag of that name added in v0.43.0 is cloud-only (see *Cloud mode*), so a local allow is still a `sbx policy allow network` call after create.
- `sbx policy rm network --resource "domain"` — remove a global rule by resource (or `--id <uuid>` from `policy ls --wide`)
- `sbx policy rm network --sandbox <SANDBOX> --resource "domain"` — remove a sandbox-scoped rule
- `sbx policy log [SANDBOX] [--type network] [--limit N] [--json] [-q]` — view allowed/blocked requests
- `sbx policy reset` — reset policies to defaults
- `sbx policy profile ...` — manage reusable policy profiles
- `sbx policy check network <host>` / `sbx policy inspect <policy-or-rule>` — (v0.35.0) test whether a request is allowed / show full detail on a policy or rule. `check` evaluates a bare host at port 443, accepts a pasted URL, and takes `--protocol tcp|udp` (v0.46.0).
- `sbx policy approval ls` / `inspect <ID>` / `respond <ID> --option allow|dismiss` — (v0.46.0) the queue of refused requests. A request with no matching rule is **still denied at once** (tested: `403` in 0s); the approval is only a record that waits for an answer, and `allow` writes a persistent rule (`policy ls --created-via approval`). ola never answers these — task egress is declared in `allowlist.txt`.
- `--method GET,HEAD [--path '/v1/**']` on `policy allow|deny|rm network` — (v0.46.0) HTTP-level rules; `policy ls --type http` lists them. Unused by ola.

> **Removal changed (v0.31.x):** `policy rm network` no longer accepts a
> positional RESOURCES argument. You MUST identify the rule with `--resource
> <csv>` and/or `--id <uuid>` (find them via `sbx policy ls --wide` — plain
> `sbx policy ls` is a summary as of v0.35.0 and does not show IDs). The old
> `sbx policy rm network -g "domain"` form is no longer valid. `--id` wants the
> **RULE_ID** column, not the rule's *name* — passing a name fails with an error
> that names the actual ID and, for a removable rule, prints the corrected
> command verbatim, so read the error rather than guessing.

#### The RESOURCES grammar (widened and tightened at v0.45.0)

RESOURCES is a comma-separated list of hostnames, domains, IP addresses **or
CIDR prefixes**. Re-adding a covered resource is idempotent (exit 0,
`Already covered: …`). Accepted forms:

| Form | Example |
|------|---------|
| exact domain | `example.com` |
| wildcard subdomain (one label) | `*.example.com` |
| multi-label wildcard (v0.45.0) | `**.example.com` |
| single-character glob (v0.45.0) | `api?.example.com` |
| character class (v0.45.0) | `api[12].example.com`, `api[!1].example.com` |
| port suffix | `example.com:443` |
| bare IPv4 | `10.0.0.5` — do **NOT** append `*.<ip>` |
| CIDR prefix (v0.45.0) | `10.0.0.0/24`, `2001:db8::1/128` |
| IPv6 | only bracketed-with-port (`[2001:db8::1]:443`) or as CIDR — **a bare IPv6 address is refused** |
| all hosts | `**` |

> **Anything outside those forms is now REJECTED, not stored (v0.45.0).** A
> bare `*`, an escaped glob character (`\*`) and any other malformed pattern
> fail the command instead of being accepted as a rule that quietly matches
> nothing. Strictly better — but it turns a former silent no-op into a hard
> failure, and **ola aborts the whole sandbox prepare on a failed rule**
> (`_ola_policy_allow` → `_ola_apply_policy` returns non-zero).
> **Fixed in ola on the v0.45.0 pass:** `_ola_apply_policy` used to append
> `*.$host` to every `allowlist.txt` entry unconditionally, so an IP literal in
> that file produced `10.0.0.5,*.10.0.0.5` — ignored by older sbx, fatal from
> v0.45.0. That loop now goes through `_ola_allow_host`, which passes an IP, a
> CIDR prefix, an IPv6, a `host:port` or an already-written wildcard straight
> through and expands only a plain domain. `docs/sandbox.md` still documents
> that file as "one host per line", so an IP there remains out of contract —
> the guard just stops an out-of-contract line from taking the sandbox with it.

**Rules are TCP-only by default (v0.45.0 surfaces this explicitly).** `sbx
policy allow network --protocol tcp|udp RESOURCES` selects the transport;
repeat or comma-separate for both (`--protocol tcp,udp`). `policy ls` gained a
matching `--protocol tcp|udp` filter. ola never passes `--protocol`, so every
rule it writes is TCP.

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
  service, e.g. the Docker-doc SSH example.) Since v0.46.0 an IP or
  `IP:port` allow rule no longer opens a *denied* hostname that resolves to
  that IP (TCP), so an IP rule cannot be used to route around a hostname deny.
- **UDP: the flat "can never be unblocked" claim is retired as of v0.45.0 —
  treat it as OPEN.** This skill said UDP and ICMP could never be allowed
  (established against v0.35.0, when the CLI offered no way to ask). v0.45.0's
  `policy allow network --help` states rules "apply to TCP by default; use
  `--protocol` to select UDP or both transports", and offers
  `--protocol tcp,udp "**"` as its own example. That is a CLI surface, **not a
  verified behaviour** — nothing was run against a live sandbox (the daemon was
  wedged for that pass), and a flag existing does not prove the proxy forwards
  UDP end to end. ICMP has no flag and is still presumed blocked. As of
  v0.47.0, UDP to multicast, link-local, unspecified and broadcast
  addresses is rejected regardless of policy (`host.docker.internal` is
  exempt); ordinary unicast UDP, and so SRV lookups, is still the open
  question.
- **So the seedlist rule stands, but for a weaker reason.** A `mongodb+srv://`
  URI needs SRV/TXT DNS over UDP; whether `--protocol udp` now makes that
  resolve in-sandbox is exactly the untested question. Keep connecting with a
  **seedlist** URI (`mongodb://h1,h2,h3/?tls=true&authSource=admin…`) — it
  works either way. Before claiming `+srv` is possible, test it: add the rule
  with `--protocol udp` and resolve the SRV record from inside a sandbox. The
  `mongo-vpn` skill still carries the old absolute wording and should be
  corrected in the same pass if this is ever confirmed.
- Sandbox egress still exits via the **host-side sbx proxy**, which follows
  the *host's* routing table — any host-level route requirement (e.g. a VPN
  bypass) still applies.
- See the `mongo-vpn` skill for the concrete MongoDB-over-VPN + sandbox recipe
  (host route + seedlist derivation) — don't duplicate it here.

#### Reaching a service running on the host (verified 2026-07-29, v0.35.0)
`host.docker.internal` resolves in-sandbox out of the box (`/etc/resolv.conf`'s
`docker.internal` search domain), so DNS is never the blocker — but the policy
engine evaluates the connection under the resource name **`localhost`**, not
`host.docker.internal`. An allow rule for `host.docker.internal` alone still
403s (`Blocked by network policy: domain localhost:...`); confirmed live that
`sbx policy allow network --sandbox <SANDBOX> localhost` is what clears it —
after that rule, a probe past the policy layer surfaced a host-side `connection
refused` (nothing listening on the probed port), proving the dial happens from
the host, not looped back inside the sandbox.

- **Connect to** `host.docker.internal:<port>` from inside the sandbox.
- **Allow rule targets** `localhost`, scoped to the sandbox
  (`--sandbox <name>`) or global — the DNS name and the policy resource name
  deliberately diverge here, unlike every other egress case in this doc.
- Same non-HTTP-TCP caveats as above apply (no SNI issue for a bare `localhost`
  rule since no port/IP suffix is used).

### Credentials
- **Preferred Claude credential: `CLAUDE_CODE_OAUTH_TOKEN`** from `claude setup-token`
  — a one-year token billed to the Claude subscription (not the API) that can
  only make model requests (no Remote Control, no claude.ai connectors; ola uses
  neither). Claude Code ranks it above any saved login. `_ola_inject_oauth_token`
  appends it to the sandbox's `~/.ola/agent.env` on every create and reconnect,
  **after** the sidecar write (which overwrites that file); unset or empty on the
  host, it is a silent no-op, never fatal and never echoed. Without it the
  `.credentials.json` copy below is the credential, as before. The token only
  helps the interactive TUI (`ct`) on a current Claude Code: 2.1.272 ignored it
  and stopped at the login screen, 2.1.296 accepts it (verified 2026-10-11) —
  the reason `_ola_update_claude` runs `claude update` on every create,
  reconnect and `ola-monitor` relaunch.
- **Claude subscription (OAuth)**: `ola-sandbox` copies `~/.claude/.credentials.json` from host into the sandbox at creation/reconnection time (via `sbx exec` + base64). No API key needed.
- **Claude Code `settings.json` is ola-owned, not copied from the host.**
  `_ola_inject_cc_settings` writes a generated file into the sandbox on every
  create/reconnect; the `cc`/`ct` backends then copy it into each per-task
  `CLAUDE_CONFIG_DIR`, **every run** (it is in `_ALWAYS_REFRESH`) — per-task
  dirs are stable across runs and never deleted, so copy-once would pin a task
  to the settings that existed the first time it ran. Edit
  `_ola_inject_cc_settings` and every task picks the change up on the next run;
  that function is the single place any of these keys is declared. Deliberately
  **no** `"sandbox"`: Claude Code's own command sandbox is redundant inside the
  docker one and would confine writes to the worktree cwd, silently blocking
  the ola-blocked marker (which lands in the agent folder, above the worktree).
  Past bypass-permissions, the remaining keys each close a way the session
  could escape or outlive the one task agent ola supervises:
  `"disableRemoteControl": true` (a task agent is unattended, so claude.ai/code,
  `claude remote-control`, `--rc`, the auto-start and the in-session toggle have
  no operator behind them; the softer `remoteControlAtStartup: false` leaves the
  toggle live, so it is the wrong knob); `"disableAgentView": true` for the
  *detached* background agents (`claude agents`, `--bg`, `/background`, the
  on-demand daemon), which outlive the `claude` process ola started and so are
  no longer bounded by the task; and
  `"env": {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}` for the *in-session*
  background subagents (`Agent(run_in_background: true)`) — a different
  mechanism with the same name, with no settings key of its own, which the flag
  both removes from the Agent tool's schema and forces synchronous. That flag
  disables Bash `run_in_background` too: intended, since a process meant to
  survive its own tool call is the long-lived-process case that must daemonize
  explicitly and be reclaimed by `run-init.sh`.
  Never copy the host `settings.json` in: it drags in personal hooks/MCP.
- **The sandbox's Claude Code is updated on every create, reconnect and
  `ola-monitor` relaunch** (`_ola_update_claude`: `claude update` inside, under
  a 180s timeout, never fatal — a failure warns and keeps the installed CLI).
  The image only carries whatever was current at build time and nothing in the
  sandbox updates it afterwards: a sandbox from `ola:0.8.0` was still on its
  build's 2.1.272 a month later. Staleness has a concrete cost — 2.1.272's
  interactive TUI ignores `CLAUDE_CODE_OAUTH_TOKEN` and stops at the login
  screen, where 2.1.296 accepts it (verified 2026-10-11). Each call site is a
  moment with no ola run in flight in that sandbox. The update persists in the
  sandbox's state across `stop`/start; the default policy already reaches the
  update host. A release still matters for a *new* sandbox's starting point:
  `make release-image` builds `--no-cache`, so every release bakes the current
  CLI.
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
  live one alone), and the `cc` and `ct` backends delete the entry keyed to a
  task's config dir whenever they refresh that dir's credentials (`ct` builds
  the same task-id-derived dirs, so it inherits the same trap). **This is
  host-only** — inside
  the sandbox there is no Keychain, so the injected file is already the sole
  credential source and the failure cannot occur. It bites `ola --skip-sandbox`,
  and only on the saved-login fallback: with `CLAUDE_CODE_OAUTH_TOKEN` (the
  one-year `claude setup-token` subscription token) in ola's environment,
  Claude Code uses the token ahead of any saved login, so the per-dir entry is
  never read and the guards have nothing to guard.
- **GitHub CLI (`gh`) auth**: on every create **and** reconnect, `ola-sandbox` also
  reads the host's `gh auth token` and injects it as `GH_TOKEN` into the sandbox
  sidecar (`~/.ola/agent.env`), then runs `gh auth setup-git` inside the sandbox
  so plain `git`-over-HTTPS (not just `gh`) works — mirroring the Claude
  credentials flow above. It also auto-allows `github.com,*.github.com` egress.
  Non-fatal when the host has no `gh` login (or no `gh` installed): a warning is
  printed and the sandbox still comes up, just without `gh`/git-over-HTTPS auth.
- **Git commit identity**: on the same create/reconnect path,
  `_ola_inject_git_identity` copies the *first* `user.name`/`user.email` from the
  host's global git config (`--get-all | head -1`, `--includes` honoured) into the
  sandbox's `git config --global`, so task commits carry the developer's identity.
  Both or neither: if either is unset on the host it warns and the image default
  `ola <ola@localhost>` (baked in `docker/Dockerfile`) stays. A repo-local `user.*`
  in the mounted checkout still wins, as on the host.
- Service secrets are held by the sbx proxy (NOT the agent / not the OS keychain); the proxy injects them into outbound API calls.

**Scoping reversed in v0.39.0 — global is now the default.** `secret` caught up
with the change `policy` made in v0.33.0: pass the service bare for a global
secret, and use the `--sandbox <name>` **flag** to scope one. Both old forms
still work but print a deprecation to **stderr** — which matters for any script
that captures stderr into its error text:

- `-g`/`--global` → `Flag --global has been deprecated, global is now the default for service secrets; omit --global, use --sandbox to target one sandbox, or use --all-sandboxes with --registry`
- positional SANDBOX (`sbx secret set my-sandbox openai`) → `Warning: positional sandbox scope is deprecated; use: sbx secret set openai --sandbox my-sandbox`. Note the usage is now `sbx secret set [SERVICE] [flags]` — the single positional is the **service**, so drop the sandbox positional before sbx removes the shim.

- `sbx secret set <service>` — store a global secret (interactive prompt)
- `echo "$KEY" | sbx secret set <service>` — non-interactive via stdin
- `sbx secret set openai --oauth` — start an OAuth flow instead of a key (openai/global only)
- `sbx secret set <service> --sandbox <name>` — sandbox-scoped secret
- `sbx secret ls [-g] [--sandbox <name>] [--service <name>]` — list stored secrets. Here `-g`/`--global` is a **filter, not scoping**, and is *not* deprecated: it narrows the listing to global secrets.
- `sbx secret rm <service> [--sandbox <name>] [-f]` — remove a secret (global by default)
- `sbx secret import [SERVICE] [--all] [--dry-run] [--force]` — import credentials detected in host env vars into the keychain. A service that already has an OAuth token is skipped (OAuth wins at runtime); `sbx secret rm <service>` first to switch to an api key.
- **Dynamic secrets (verified v0.39.0)** — store a *source* instead of a value, resolved on the host when needed: `--ref` takes a 1Password `op://` reference or an AWS Secrets Manager ARN (the `op`/`aws` CLI must be installed and authenticated), `--command` uses a shell command's stdout (`sbx secret set github --command 'gh auth token'`; since v0.46.0 the command runs from a fresh temporary directory, so use absolute paths for any helper script). `--refresh` sets the cache policy (`on-demand`, or a duration; default 55m); `--no-verify` skips the store-time source check.
- `sbx secret set-custom --host <pat> --env <VAR> --value <secret>` — (experimental) a secret for a service sbx doesn't know about. The sandbox only ever sees a generated **placeholder** in `<VAR>`; the proxy swaps in the real value on outbound requests to `--host` (repeatable; `*` matches one label, `**` any number). Remove with `sbx secret rm --placeholder <value>`.
- Services (gained `copilot` and `devin` in v0.43.0; **unchanged at v0.45.0**): `anthropic, copilot, cursor, devin, droid, github, google, groq, mistral, nebius, openai, openrouter, xai` — re-check with `sbx secret set --help` after upgrades. Note this list is **not** the same as the placeholder keys sbx injects into a sandbox: `docker/placeholder-api-keys.txt` is derived from what was *observed* inside a live sandbox (it has no `GROQ_API_KEY`/`OPENROUTER_API_KEY` despite both being long-standing services), so a new service here is a prompt to re-check a live sandbox's `env`, not to edit that file blind. Not re-checked at v0.43.0 — the daemon was down.
- `--registry-auth-endpoint URL` (v0.43.0) — trust an exact HTTPS URL for a registry whose Bearer auth endpoint lives on a different hostname than the registry itself.
- **Registry secrets** (e.g. `ghcr.io`) authenticate private template/kit pulls:
  `gh auth token | sbx secret set --registry ghcr.io --password-stdin`. Host-only
  by default — the credential is used for host-side pulls and never enters a
  sandbox. Use **`--all-sandboxes`** (this is what `-g` used to mean here) to also
  inject it into every new sandbox's registry login, or `--sandbox <name>` for one.
  Remove with `sbx secret rm --registry ghcr.io -f` (add `--all-sandboxes` to drop
  only the injected copy). `docs/sandbox.md` uses the host-only form, which is
  still correct.

> **ola is unaffected by this reversal** — ola never calls `sbx secret`. Claude
> OAuth and `gh` auth are injected by `ola-sandbox` directly (above), and the
> only `sbx secret` line in the repo is the host-only registry example in
> `docs/sandbox.md`, which needs no change.

### `ola-monitor` (host-side launcher-watcher)
`ola-monitor` (in `ola.sh`) wraps `ola-sandbox`'s create/reconnect + credential-inject
path (`_ola_sandbox_prepare`) plus a non-interactive `sbx exec` of `ola <args>` to
keep an unattended ola run going across the two stops it cannot clear from inside
the sandbox, each signalled by its own host-visible marker under
`<agent-folder>/monitor/`:

- **`auth-escalation.json`** (ola exits 40) — re-pull Keychain credentials,
  re-inject them into the sandbox, relaunch. Thrash-guarded: repeated re-heals in
  a short window mean cc-credentials isn't fixing it (most often a concurrent
  rotator, but not always), so it stops and notifies. A dead Keychain token
  notifies rather than loops. **With `CLAUDE_CODE_OAUTH_TOKEN` set this path is
  not taken:** the token outranks every saved login, so re-copying Keychain
  credentials cannot help. The watcher deletes the marker and stops (exit 1)
  with instructions instead — run `claude setup-token`, put the new token in
  `CLAUDE_CODE_OAUTH_TOKEN`, open a new shell, re-run `ola-monitor`.
- **`rate-limit.json`** (ola exits 41) — nothing is broken; the subscription
  window just has to run out. Sleep to the marker's `resets_at` (floored at 60s,
  so a stale or missing epoch can't hot-spin), re-pull credentials — a five-hour
  window outlives the OAuth token, so skipping this would bounce straight into an
  auth escalation — then relaunch. Deliberately *not* thrash-guarded: a plan
  outliving several windows is the case this exists for.

Claude Code gives no notice as an env-var token nears expiry, so
`_ola_check_token_age` (called at the top of `_ola_sandbox_prepare`, i.e. at
every launch) remembers when each token was first seen and warns from 335 days
after that (and again, louder, from 365). The record is
`${XDG_CONFIG_HOME:-~/.config}/ola/oauth-tokens`, one line per token:
fingerprint (a truncated SHA-256), first-seen epoch and date — never the token
itself. Never fatal.

Both markers are cleared before the launch loop starts, so only one written by
*this* invocation is ever trusted. Because `sbx exec` never sources
`~/.bashrc`, `ola-monitor`'s exec line also re-applies what login shells get for
free: `SANDBOX=1`, and stripping the placeholder provider API keys `sbx` injects
into every sandbox (`sbx secret import`'s services above) via `env -u` — a live
placeholder `ANTHROPIC_API_KEY` makes the `cc` backend fail with `Invalid API key`
regardless of OAuth token freshness, which looks like but isn't the concurrent-
rotator case. Both env fixups read `docker/placeholder-api-keys.txt` /
`_ola_placeholder_keys` so the key list is declared once, not duplicated between
the Dockerfile's `~/.bashrc` and `ola.sh`. Narrow scope — no progress reporting
of its own (that's `ola-top`). Full contract in CLAUDE.md.

`_ola_sandbox_prepare` (and `ola-sandbox`) take the **agent folder as an
optional second argument**, defaulting to `../agent` — a project may hold one
agent folder per epic, and `ola-monitor` passes whatever it resolved from ola's
own `-f`. If that folder holds a **`provision.sh`**, prepare runs it inside the
sandbox on every create and reconnect (base64 through `sbx exec`, so an agent
folder outside the bind-mounted project dir still works), aborting on a non-zero
exit. That is the seam for per-project tooling the generic image does not ship;
apt-based scripts there must handle the boot-time apt lock above.

**Two gotchas fixed 2026-07-29** (both in `_ola_sandbox_prepare`/`ola-monitor`,
`ola.sh`): (1) the create-vs-reconnect check used a plain `grep -q "$name"`
against `sbx ls` output — an unanchored substring match, so an unrelated,
already-running sandbox whose name merely *contains* the target (e.g.
`reference-checker` against `reference-checker-dashboard`) false-positived
into the reconnect branch, `sbx create` was never called, and every later `sbx
exec "$name"` failed with `no sandbox named`. Fixed to anchor on the SANDBOX
column (`grep -qE "^${name}[[:space:]]"`). (2) `ola-monitor`'s only signal
that `ola` hit a real auth escalation is "does the marker file exist" — a
marker left behind by an earlier, unrelated invocation (e.g. one interrupted
before its own cleanup) was indistinguishable from a fresh one, so any
unrelated `sbx exec`-level failure (like bug 1) got misdiagnosed as an auth
escalation and pointlessly re-healed credentials that were never the problem.
Fixed by clearing any marker present before the launch loop starts, so only a
marker written during *this* invocation's own run is ever trusted. The
rate-limit marker added in 2026-08 is swept on the same path, where a stale one
would be worse still: the watcher would sleep towards an epoch that passed days
ago and then relaunch a run that failed for an unrelated reason.

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
- Snapshot a running sandbox into a reusable template: `sbx template save` / `sbx template ls` / `sbx template inspect` (full metadata for one) / `sbx template rm` / `sbx template load`
- Private images: store a registry secret first (see Credentials → Registry secrets)
- **Kits (experimental):** `--kit <ref>` on `create`/`run` (directory, ZIP, or OCI; repeatable) layers extra tooling/policy onto a sandbox. Since v0.34.0, kit installs are restricted to an allowlist configured via `sbx settings set kit.allowedSources`; private kit artifacts pull via the same registry secrets as templates. ola does not use kits — this is here so an unexpected `--kit`/allowlist error is legible.

### MCP servers and declarative environments (verified v0.43.0, unused by ola)

Two command families ola does not touch. Documented so an unexpected flag or
error is legible, not as a recommendation to adopt them.

- **`sbx mcp`** — `add` / `auth` / `inspect` / `load` / `ls` / `rm` register MCP
  servers for sandbox sessions; `sbx mcp ls` groups them by serving gateway and
  `sbx mcp load` pushes an already-registered server into a *running* sandbox.
  Pairs with `--static-mcp a,b` on `create`/`run`, which fixes a sandbox's MCP
  set at creation — that set **cannot be changed on re-attach**.
- **`sbx env`** — experimental; `create` / `exec` / `plan` / `rm` / `run` drive a
  sandbox declared in an `sbxenv.yaml` (agent, mixin kits, mounts, env vars,
  secrets, per-service credential bindings). Secrets are provisioned at the
  environment's sandbox scope so `sbx env rm` can tear down everything it
  created. Note the name collision with the unrelated `-e`/`--env` flags on
  `create`/`run`. Much larger at v0.43.0: a terraform-shaped **plan/approve**
  model (`+ add`, `~ change`, `- destroy`, `> run`, `! forget`; `sbx env plan`
  prints it and changes nothing; `--auto-approve`/`-y` for a non-TTY); an
  `args:` block with `${{ env.args.NAME }}` interpolation and `--env-arg`; and
  a `lifecycle:` block of `initialize`/`postCreate`/`preRemove` commands that
  run **on the host, with your own privileges**, not in the sandbox — which is
  why an environment declaring any of them re-asks on every invocation, and
  why `--skip-host-commands` exists. An environment file inside its own mount
  is bound read-only at its path unless `sandboxOptions.writableEnvFiles:
  true`, so an agent cannot rewrite what the next plan asks about.

## Debugging
- `sbx diagnose` — first stop for installation/daemon/connectivity problems
- `sbx policy log [SANDBOX]` — check what the proxy is blocking
- `sbx exec -it <name> bash` — inspect sandbox state interactively
- Clock drift after sleep? `sbx stop <name>` then `sbx run --name <name>` (reconnect by sandbox name; positional re-attach deprecated in v0.33.0). Published ports are restored on restart as of v0.34.0.
- Daemon issues? `sbx daemon status` / `sbx daemon start|stop` / **`sbx daemon restart` (new in v0.45.0)** / `sbx daemon log-level` (v0.35.0 top-level command). A `database already in use` error from a policy/create call means another process (or a live sandbox) holds the daemon DB.
- **`ensure daemon: sandboxd (PID …) remained running but did not respond within 10s` has (at least) two causes, and they look identical.** In both, `sbx daemon status` reports `Status: stopped` and every command (`sbx ls`, `sbx policy …`, `sbx settings list`) fails the same way.
  1. **A stale daemon after an sbx upgrade.** The new client cannot talk to the still-running old `sandboxd` and does not kill it. Observed v0.39.0 → v0.43.0 and again v0.43.0 → v0.45.0. **`sbx daemon restart` is the v0.45.0 fix** (kill the named PID or restart Docker Desktop on older clients). Nothing is lost, but the restart may drop running sandboxes, so stop an in-flight ola run first.
  2. **The caller simply cannot reach the daemon socket** — e.g. an agent harness running commands inside its own OS-level sandbox (macOS Seatbelt) that denies the unix socket under `~/Library/Application Support/com.docker.sandboxes/`. **This was mistaken for cause 1 during the v0.45.0 pass**: a restart reported success, the daemon came up healthy, and `sbx ls` kept printing the same error with the *new* PID — while the identical command outside the restricted shell returned `No sandboxes found.` The tell is a **fresh** PID in the message: a just-restarted daemon cannot be stale. `sbx daemon restart` itself fails distinctly here (`force kill process N: operation not permitted; daemon left running, not restarted`), because SIGKILL to a foreign PID is what gets denied first.
  Note **an `ola-monitor` relaunch across an upgrade hits this at `_ola_sandbox_prepare`'s `sbx ls` probe.** As of the v0.45.0 pass that probe no longer reports it as "not authenticated" — it matches the message and points at `sbx daemon restart` instead. Read the stderr, not the summary.
- Corrupted state? `sbx reset` (add `--preserve-secrets` to keep stored secrets)
- LLM calls fail with `Invalid port: ':1]'` (litellm/httpx)? sbx **v0.31.0** added a
  bracketed `[::1]` entry to the injected `NO_PROXY` ("Add bracketed [::1] to
  NO_PROXY for IPv6 loopback"). httpx parses each no_proxy entry as a URL and
  rejects the bracketed form, killing every LLM call before egress. Not a policy
  problem — the proxy env is sbx-injected, not in ola's sidecar `agent.env`. ola
  scrubs it at startup (`ola.sandbox.sanitize_proxy_env`, called in `cli.main`);
  check `env | grep -i proxy` inside the sandbox to see the raw value. At
  v0.46.0 the injected value is unbracketed again (`localhost,127.0.0.1,::1,
  gateway.docker.internal`), so the scrub does nothing there — keep it for
  older sbx.

## Key Differences from `docker sandbox`
- No manual proxy config needed (auto-configured)
- Claude credentials: `~/.claude/.credentials.json` copied into sandbox by `ola-sandbox` (OAuth token, not API key)
- `balanced` policy replaces manual `--allow-host` chains
- Multiple mounts: `sbx run claude ~/a ~/b:ro`
- Reconnect to an existing sandbox: `sbx run --name <SANDBOX>` (agent read from spec). The positional `sbx run <SANDBOX>` form was deprecated in v0.33.0; `sbx run claude --name <n>` still creates-or-runs.
- `sbx version` reports client+server version (there is no `--version` flag)
