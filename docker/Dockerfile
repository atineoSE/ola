FROM docker/sandbox-templates:shell

# Install Claude Code CLI (native installer — no Node.js required)
RUN curl -fsSL https://claude.ai/install.sh | bash

# Claude Code config: bypass onboarding and enable skip-permissions
RUN echo '{"hasCompletedOnboarding": true}' > ~/.claude.json && \
    mkdir -p ~/.claude/settings && \
    echo '{"permissions":{"allow":["*"],"deny":[]}}' > ~/.claude/settings/settings.json

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
    echo "alias claude-yolo='claude --dangerously-skip-permissions'" >> ~/.bashrc

# OpenHands: on login, install config from workspace and register alias
RUN mkdir -p ~/.openhands && \
    echo 'for f in agent_settings.json cli_config.json; do [ -f ".oh-$f" ] && mv ".oh-$f" ~/.openhands/"$f"; done' >> ~/.bashrc && \
    echo "alias oh='openhands --llm-approve --exit-without-confirmation'" >> ~/.bashrc

# Verify all tools and config are in place
RUN set -e && \
    uv --version && \
    claude --version && \
    openhands --version && \
    git config --global user.name | grep -q ola
