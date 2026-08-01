FROM docker/sandbox-templates:shell-docker

# jq: ola.sh's _ola_inject_oh_settings patches the rotating LLM endpoint into
# ~/.openhands/agent_settings.json with jq before each parallel `oh` run; absent
# jq it silently falls back to a possibly-stale base_url. Install it explicitly
# so the OpenHands path is robust rather than degraded.
#
# gh (GitHub CLI): ola.sh's _ola_inject_gh injects the host's `gh auth token`
# into the sandbox and runs `gh auth setup-git` so both `gh` and plain
# git-over-HTTPS work. Not in Ubuntu's default apt repos, so add GitHub's
# official apt repository first (keyring + sources list, per
# https://cli.github.com/packages).
RUN sudo mkdir -p -m 755 /etc/apt/keyrings && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
        sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && \
    sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    sudo mkdir -p -m 755 /etc/apt/sources.list.d && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
        sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    sudo apt-get update && \
    sudo apt-get install -y --no-install-recommends jq gh && \
    sudo rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (native installer — no Node.js required)
RUN curl -fsSL https://claude.ai/install.sh | bash

# Claude Code config: bypass onboarding. Written to BOTH the legacy
# ~/.claude.json and the config-dir form ~/.claude/.claude.json: parallel mode
# sets CLAUDE_CONFIG_DIR per task and bootstraps each task dir from
# ~/.claude/.claude.json (see agents/claude_code.py), so the legacy path alone
# never reaches a parallel worker.
RUN mkdir -p ~/.claude && \
    echo '{"hasCompletedOnboarding": true}' > ~/.claude.json && \
    echo '{"hasCompletedOnboarding": true}' > ~/.claude/.claude.json

# Install Codex CLI (npm package — Node 20 is already in the base image,
# with NPM_CONFIG_PREFIX=/usr/local/share/npm-global owned by the agent user)
RUN npm install -g @openai/codex

# Install uv (Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh && \
    sh /tmp/uv-install.sh && rm /tmp/uv-install.sh

# Install OpenHands CLI
RUN $HOME/.local/bin/uv tool install openhands --python 3.12

# Playwright (headless Chromium): a project's own CLAUDE.md sometimes carries
# browser-screenshot instructions written for a macOS host (e.g. hardcoding
# /Applications/Google Chrome.app), which doesn't exist in this Linux sandbox
# — an in-sandbox task then has no way to actually verify a UI change and can
# only report "not available." Not `apt install chromium`: on modern Ubuntu
# that package is a snap-wrapper stub that fails outright without snapd
# (unavailable in a container). Playwright's `install --with-deps` instead
# downloads a self-contained Chromium build plus the system libs it needs, so
# it works regardless of the base image's apt-repo chromium situation.
# `chromium-headless` is a stable wrapper script so a Linux fallback command
# doesn't need to track Playwright's revision-numbered install path (it
# changes across Playwright versions).
RUN $HOME/.local/bin/uv tool install playwright --python 3.12 && \
    $HOME/.local/bin/playwright install --with-deps chromium && \
    printf '#!/bin/bash\nexec "$(find "$HOME/.cache/ms-playwright" -maxdepth 3 -type f -iname chrome -perm -u+x | head -1)" --headless=new --disable-gpu --no-sandbox "$@"\n' | \
        sudo tee /usr/local/bin/chromium-headless > /dev/null && \
    sudo chmod +x /usr/local/bin/chromium-headless

# Copy project and install ola (brings in openhands-sdk as a dependency)
COPY --chown=1000:1000 . /tmp/ola-src
RUN $HOME/.local/bin/uv tool install /tmp/ola-src && rm -rf /tmp/ola-src

# Git identity so commits work without per-repo config
RUN git config --global user.name "ola" && \
    git config --global user.email "ola@localhost" && \
    git config --global --add safe.directory '*'

# Unset proxy-managed API keys injected by sbx — they are placeholders that
# confuse downstream tools; Claude prefers ANTHROPIC_API_KEY over its own OAuth
# credentials.json whenever both are present, so a placeholder left in scope
# silently breaks the `cc` backend with "Invalid API key" regardless of how
# fresh the OAuth token is. OpenHands gets its key from the agent .env via ola.
#
# docker/placeholder-api-keys.txt is the single source of truth for this list
# — ola.sh's ola-monitor reads the same file to strip these vars from its
# non-interactive `sbx exec` launch, which (unlike an interactive attach)
# never sources ~/.bashrc and so would otherwise miss this unset entirely.
COPY --chown=1000:1000 docker/placeholder-api-keys.txt /tmp/placeholder-api-keys.txt
RUN echo 'export SANDBOX=1' >> ~/.bashrc && \
    echo "unset $(tr '\n' ' ' < /tmp/placeholder-api-keys.txt)" >> ~/.bashrc && \
    rm /tmp/placeholder-api-keys.txt && \
    echo "alias claude-yolo='claude --dangerously-skip-permissions'" >> ~/.bashrc && \
    echo "alias cx='codex exec --json --ephemeral'" >> ~/.bashrc

# OpenHands: on login, install config from workspace and register alias
RUN mkdir -p ~/.openhands && \
    echo 'for f in agent_settings.json cli_config.json; do [ -f ".oh-$f" ] && mv ".oh-$f" ~/.openhands/"$f"; done' >> ~/.bashrc && \
    echo "alias oh='openhands --llm-approve --exit-without-confirmation'" >> ~/.bashrc

# Verify all tools and config are in place. Beyond tool presence, smoke-test
# `git worktree` — the parallel-agents flow runs every task in its own worktree
# (see src/ola/worktree.py), so an old or broken git here would fail every run.
RUN set -e && \
    uv --version && \
    claude --version && \
    openhands --version && \
    codex --version && \
    docker --version && \
    jq --version && \
    gh --version && \
    playwright --version && \
    git config --global user.name | grep -q ola && \
    d="$(mktemp -d)" && cd "$d" && git init -q && git config user.email ola@localhost && \
    git config user.name ola && git commit -q --allow-empty -m init && \
    git worktree add -q wt HEAD && git worktree remove wt && cd / && rm -rf "$d" && \
    d2="$(mktemp -d)" && chromium-headless --screenshot="$d2/smoke.png" --window-size=800,600 about:blank && \
    [ -s "$d2/smoke.png" ] && rm -rf "$d2"
