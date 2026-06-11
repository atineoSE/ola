FROM docker/sandbox-templates:shell-docker

# jq: ola.sh's _ola_inject_oh_settings patches the rotating LLM endpoint into
# ~/.openhands/agent_settings.json with jq before each parallel `oh` run; absent
# jq it silently falls back to a possibly-stale base_url. Install it explicitly
# so the OpenHands path is robust rather than degraded.
RUN sudo apt-get update && \
    sudo apt-get install -y --no-install-recommends jq && \
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

# Copy project and install ola (brings in openhands-sdk as a dependency)
COPY --chown=1000:1000 . /tmp/ola-src
RUN $HOME/.local/bin/uv tool install /tmp/ola-src && rm -rf /tmp/ola-src

# Git identity so commits work without per-repo config
RUN git config --global user.name "ola" && \
    git config --global user.email "ola@localhost" && \
    git config --global --add safe.directory '*'

# Unset proxy-managed API keys injected by sbx — they are placeholders that
# confuse downstream tools. Claude uses OAuth credentials instead; OpenHands
# gets its key from the agent .env via ola.
RUN echo 'export SANDBOX=1' >> ~/.bashrc && \
    echo 'unset ANTHROPIC_API_KEY OPENAI_API_KEY GEMINI_API_KEY GOOGLE_API_KEY XAI_API_KEY MISTRAL_API_KEY NEBIUS_API_KEY' >> ~/.bashrc && \
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
    git config --global user.name | grep -q ola && \
    d="$(mktemp -d)" && cd "$d" && git init -q && git config user.email ola@localhost && \
    git config user.name ola && git commit -q --allow-empty -m init && \
    git worktree add -q wt HEAD && git worktree remove wt && cd / && rm -rf "$d"
