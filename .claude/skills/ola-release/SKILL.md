---
name: ola-release
description: Cut a new ola release — bump the version, publish the multi-arch sandbox template image to GHCR, and tag the repo, so that a fresh install on any machine pulls a sandbox image matching its CLI. Use when the user says "release ola", "cut a release", "make a new version", "publish the image", or asks how ola versioning and the sandbox image line up.
version: 1.1.0
---

# ola-release

Cutting an ola release means publishing **two artifacts that must agree**:

1. the Python package (`uv tool install .` → the `ola`, `ola-top`, `ola-dashboard` CLIs), and
2. the **sandbox template image** that `ola-sandbox` hands to `sbx create --template`.

They agree because of one rule, and the whole design exists to protect it:

> **`pyproject.toml`'s `version` is the only place a version is written.**
> `ola --version` reads it via `importlib.metadata`; `ola.sh`'s `_ola_image_tag`
> shells out to `ola --version` and pulls `ghcr.io/atineose/ola:<that version>`.
> Bumping `pyproject.toml` therefore re-points the sandbox image too.

If you ever find yourself adding a second place that spells out the version, stop —
that is the bug this design was built to prevent.

## The contract

| | |
|---|---|
| Version source | `pyproject.toml` → `[project] version` |
| Registry | GHCR: `ghcr.io/atineose/ola` (override with `OLA_IMAGE_REPO` / `IMAGE_REPO`) |
| Tags pushed | `X.Y.Z` (immutable — what installs resolve) **and** `latest` (moving pointer, fallback only) |
| Platforms | `linux/amd64,linux/arm64` |
| Git tag | `vX.Y.Z`, pushed **after** the image is verified |
| Automation | Local, `make`-driven. No CI exists in this repo. |

### Why GHCR and not the local Docker store

`sbx` pulls templates from an **OCI registry**; it does not read the local Docker
daemon's image store. So "publishing" *is* "pushing to a registry" — there is no
share-a-tarball path. The one exception is the dev flow (`make sandbox-dev`),
which `docker save`s into `sbx template load`; that is why iterating locally works
with no registry at all.

### Why multi-arch

Building on Apple Silicon yields an **arm64-only** manifest. An x86 host then fails
at `sbx create` with a hard "no matching manifest" error — not a slow fallback. The
base image (`docker/sandbox-templates:shell-docker`) publishes both `linux/amd64`
and `linux/arm64`, so both legs build. The amd64 leg emulates on an Apple Silicon
host, which roughly doubles build time; that cost is the price of the release being
installable anywhere.

### Image resolution precedence (`ola.sh`, `_ola_sandbox_prepare`)

```
$OLA_SBX_IMAGE                       # explicit override, wins outright
  → ola:dev                          # if `make sandbox-dev` loaded it into sbx's template store
    → $OLA_IMAGE_REPO:$(ola --version)   # the released, version-pinned image
      → $OLA_IMAGE_REPO:latest       # only when `ola` is not on PATH / prints an odd format
```

The `ola:dev` rung sits **above** the released image on purpose: a developer with a
local build always gets their local build, never a surprise pull.

## Release procedure

Run these in order. Each step gates the next; do not skip the verification.

### 1. Pre-flight

```bash
git status --porcelain      # must be empty — the image COPYs the working tree verbatim
git checkout main && git pull
make test                   # python + shell suites
make dashboard-test         # SPA lint + unit tests
```

A dirty tree is a hard stop, not a warning: `docker/Dockerfile` does `COPY . /tmp/ola-src`,
so uncommitted edits would silently ship inside the image while the git tag claims
otherwise. `make release-image` enforces this too.

### 2. Choose the version

Semver against the **user-visible surface**: the `ola`/`ola-top`/`ola-dashboard` CLI
flags, the agent-folder contract (`PLAN.md` semantics, folder ordering), the
`ola.sh` helper names and env knobs, and the sandbox image contents.

- **patch** — bug fixes, doc/skill edits, internal refactors
- **minor** — new flags, a new agent backend, new tooling baked into the image
- **major** — a changed folder contract or a removed/renamed CLI flag or helper

Pre-1.0, treat a breaking change as a **minor** bump and say so loudly in the notes.

### 3. Bump

```bash
# edit pyproject.toml: version = "X.Y.Z"
uv lock                     # refreshes ola's own pinned version in uv.lock
uv tool install --editable . # so `ola --version` on this host reports X.Y.Z
ola --version               # → "ola X.Y.Z"; ola.sh reads exactly this
```

Do **not** hand-edit `uv.lock`. Do **not** touch skill `version:` frontmatter here —
skill versions are independent semver, bumped by the change that edits the skill
(see CLAUDE.md, *Treat skills as code*).

### 4. Commit the bump

```bash
git commit -am "release: vX.Y.Z"
```

Commit **before** building. The image must be built from the exact tree the git tag
will point at, or the tag is a lie about what shipped.

### 5. Build + push the image

Pushing needs the **`write:packages`** scope. The `gh` OAuth token does *not* carry
it (`gh auth status` typically shows only `gist, read:org, repo`), so
`gh auth token | docker login` is **not** enough. Use a classic PAT scoped to
`write:packages` — create it at github.com/settings/tokens, then:

```bash
echo <TOKEN> | docker login ghcr.io -u atineoSE --password-stdin
```

Prefer the PAT over `gh auth refresh -s write:packages`: the latter rotates the
token `_ola_inject_gh` pushes into every sandbox, so every live sandbox would need
reconnecting, and it hands package-write rights to every agent.

Verify the credential *before* building — a stale one authenticates for pull and
only fails at the final push, after the slow build. **Print the verdict, never the
response body**: GHCR answers a `push,pull` scope request with a bearer token that
is a base64 of the PAT itself, so echoing it puts a live `write:packages`
credential into your terminal scrollback (and into the transcript of any agent
running this for you). Ask `jq` whether the token exists instead of showing it:

```bash
CRED=$(echo ghcr.io | docker-credential-osxkeychain get)
U=$(jq -r .Username <<<"$CRED"); P=$(jq -r .Secret <<<"$CRED")
curl -s -u "$U:$P" 'https://ghcr.io/token?scope=repository:atineose/ola:push,pull&service=ghcr.io' \
  | jq -e 'has("token")' >/dev/null && echo "push scope: OK" || echo "push scope: DENIED"
unset CRED U P
```

`DENIED` means the credential lacks `write:packages` — go back to the PAT step
above. If a token body ever does reach a log or a shared transcript, treat the PAT
as compromised and rotate it at github.com/settings/tokens; re-`docker login`
afterwards.

Then:

```bash
make release-image          # buildx, both platforms, tags X.Y.Z and latest, --push
```

`release-image` creates a `docker-container` buildx builder named `ola-release` on
first use (the default `desktop-linux` driver cannot emit multi-platform manifests).
Expect this to take a while — `--no-cache` plus an emulated amd64 leg.

It also **tears that builder down when the build finishes**, pass or fail
(`release-builder-clean`, which you can also run on its own). This is not
tidiness — it is the difference between a flat disk and a full one. The builder
keeps its cache in a Docker volume that only ever grows, and since the build
passes `--no-cache` nothing ever reads it: one release cycle costs ~25 GB, and
the volume had reached **73 GB** before anyone looked. It stays invisible to
`docker system df` the whole time, because a running builder container makes its
volume count as *active*, not reclaimable. The next `release-image` recreates
the builder, so nothing is lost but the dead weight.

Two details the target encodes, both learned the hard way: `docker buildx rm`
can **report a timeout and still complete** — deleting tens of GB outlives the
API deadline — so the target checks for a surviving volume afterwards rather
than trusting the exit code, and warns loudly instead of silently retrying a
hardcoded volume name. And cleanup never fails the release: the build's exit
status is captured before teardown and re-raised after it, so a push that
succeeded stays succeeded and a build that failed still fails.

### 6. Verify the push

```bash
make release-verify         # asserts the manifest lists every target platform
```

Confirm the image really is anonymously pullable (i.e. the package is public — a
private one fails only on the *consumer's* machine, which is the worst place to
find out):

```bash
TOK=$(curl -s 'https://ghcr.io/token?scope=repository:atineose/ola:pull&service=ghcr.io' | jq -r .token)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOK" \
  -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://ghcr.io/v2/atineose/ola/manifests/X.Y.Z   # want 200
```

Do this with `curl`, not `docker`/`buildx` — those prefer a stored `ghcr.io`
credential, so a *stale* one yields a 403 that looks exactly like "the package is
private" when the package is actually fine.

Check the image contents on **both** arches:

```bash
for p in linux/arm64 linux/amd64; do
  docker run --rm --platform "$p" ghcr.io/atineose/ola:X.Y.Z bash -lc 'ola --version'
done
```

The emulated amd64 run is slow — allow several minutes on first pull, and don't
mistake a 2-minute timeout for a failure. Pipe through `tail -1`: the OpenHands
banner will otherwise bury the version line.

Finally, the real consumer path — a genuine sandbox created from the registry
image:

```bash
cd <project-checkout>   # must have ../agent alongside it
zsh -c 'source ./ola.sh && OLA_SBX_IMAGE="ghcr.io/atineose/ola:X.Y.Z" _ola_sandbox_prepare release-check'
sbx exec release-check bash -lc 'ola --version'   # must print X.Y.Z
sbx rm -f release-check
```

Use the `OLA_SBX_IMAGE` override rather than `sbx template rm ola:dev` — the
override wins outright and costs nothing, whereas deleting the dev template forces
a full `make sandbox-dev` rebuild afterwards. And drive `_ola_sandbox_prepare` +
`sbx exec` rather than `ola-sandbox`, which ends by attaching interactively.

Together these catch the failure modes that matter: a private package, a missing
arch, or a CLI/image version mismatch.

### 7. Tag and push

```bash
git tag -a vX.Y.Z -m "ola vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

Tag **last**. A tag whose image failed to publish is worse than no tag: it advertises
a release that cannot be installed.

### 8. Release notes

```bash
gh release create vX.Y.Z --title "ola vX.Y.Z" --generate-notes
```

Beyond the generated commit list, call out by hand anything that changes how a user
*runs* ola: new/changed CLI flags, agent-folder contract changes, new env knobs, new
tooling in the sandbox image, and any manual migration step.

## Consumer side — what "install the right version" means

On a fresh machine:

```bash
git clone git@github.com:atineoSE/ola.git && cd ola
git checkout vX.Y.Z
uv tool install .                        # ola --version → X.Y.Z
ln -sf "$PWD/ola.sh" ~/.ola.sh           # and source it from .zshrc
ola-sandbox my-sandbox                   # pulls ghcr.io/atineose/ola:X.Y.Z
```

No image tag is ever typed. The checkout determines the package version, and the
package version determines the image tag.

**If the GHCR package is private**, every consumer must first store a registry
secret or the pull fails:

```bash
gh auth token | sbx secret set --registry ghcr.io --password-stdin
```

Making the package **public** removes that step entirely, and is the recommended
setting for a released image. Set it once in the GitHub package settings; it is not
something the release procedure can toggle.

## Checklist

- [ ] Clean tree on `main`, `make test` and `make dashboard-test` green
- [ ] `pyproject.toml` version bumped; `uv lock` run; `ola --version` reports it
- [ ] Bump committed **before** the build
- [ ] `make release-image` pushed `X.Y.Z` **and** `latest`, and reported the builder torn down
- [ ] `make release-verify` lists both platforms
- [ ] Real sandbox created from the registry image reports the right `ola --version`
- [ ] `vX.Y.Z` tag created and pushed **after** verification
- [ ] Release notes call out user-visible changes
- [ ] `README.md` / `docs/sandbox.md` updated if the install or sandbox flow changed

## Out of scope

- **PyPI.** ola is installed from a checkout (`uv tool install .`), not from an index.
  Nothing here publishes a wheel.
- **The dashboard SPA.** `ola-dashboard` resolves its assets relative to the repo
  checkout (`repo_dist_dir()` in `src/ola/dashboard/server.py`), and `dashboard/dist`
  is in `.dockerignore`. It is a dev/demo tool run from the checkout after
  `make dashboard`; a released install does not ship a built SPA. Do not claim
  otherwise in release notes.
- **Skill versions.** Independent semver, bumped alongside the change they describe.
- **`sbx` itself.** Its contract is pinned in the `sbx` skill and versioned separately.

## Failure modes seen before

| Symptom | Cause | Fix |
|---|---|---|
| `sbx create` → manifest not found for the host arch | Single-arch (arm64) push from a Mac | Re-run `make release-image`; confirm with `make release-verify` |
| `sbx create` → unauthorized pulling from ghcr.io | GHCR package is private and no registry secret stored | Make the package public, or `gh auth token \| sbx secret set --registry ghcr.io --password-stdin` |
| `imagetools inspect` → 403 on a package you know is public | Docker preferred a **stale** stored `ghcr.io` credential over anonymous access | Re-`docker login`; confirm the package is fine with the anonymous `curl` probe in step 6 |
| `docker login` succeeds but `--push` is denied | Token lacks `write:packages` (the `gh` OAuth token does not have it) | Use a classic PAT scoped `write:packages`; run the push-scope probe in step 5 first |
| Sandbox reports a different `ola --version` than the host | A stale `ola:dev` template outranks the registry image | `sbx template rm ola:dev`, recreate the sandbox |
| Image contains uncommitted changes | Built from a dirty tree | `make release-image` refuses this; never bypass the guard |
| `docker buildx build --platform` → "multiple platforms not supported" | Default `desktop-linux` driver | Use the `ola-release` container builder the make target creates |
