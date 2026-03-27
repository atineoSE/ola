FROM docker/sandbox-templates:shell

# Install uv (Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Claude Code CLI
RUN curl -fsSL https://claude.ai/install.sh | sh


# Install OpenHands CLI
RUN $HOME/.local/bin/uv tool install openhands --python 3.12

# Copy project and install ola (brings in openhands-sdk as a dependency)
COPY --chown=1000:1000 . /tmp/ola-src
RUN $HOME/.local/bin/uv tool install /tmp/ola-src && rm -rf /tmp/ola-src

# Laminar: point at host machine from inside the sandbox
ENV LMNR_BASE_URL=http://host.docker.internal

# Git identity so commits work without per-repo config
RUN git config --global user.name "ola" && \
    git config --global user.email "ola@localhost" && \
    git config --global --add safe.directory '*'

# Claude Code: skip onboarding, bypass permissions
RUN mkdir -p ~/.claude && \
    echo '{"hasCompletedOnboarding":true}' > ~/.claude.json && \
    echo '{"permissions":{"defaultMode":"bypassPermissions"}}' > ~/.claude/settings.json
COPY --chown=1000:1000 docker/NETWORK-POLICY.md /home/agent/.claude/CLAUDE.md

# Claude Code: on login, install credentials from workspace and register alias
RUN echo 'unset ANTHROPIC_API_KEY' >> ~/.bashrc && \
    echo 'if [ -f .credentials.json ]; then mv .credentials.json ~/.claude/.credentials.json && chmod 600 ~/.claude/.credentials.json; fi' >> ~/.bashrc && \
    echo "alias claude-yolo='claude --dangerously-skip-permissions'" >> ~/.bashrc

# OpenHands: on login, install config from workspace and register alias
RUN echo 'mkdir -p ~/.openhands' >> ~/.bashrc && \
    echo 'for f in agent_settings.json cli_config.json; do [ -f ".oh-$f" ] && mv ".oh-$f" ~/.openhands/"$f"; done' >> ~/.bashrc && \
    echo "alias oh='openhands --llm-approve --exit-without-confirmation'" >> ~/.bashrc

# Verify all tools and config are in place
RUN set -e && \
    uv --version && \
    claude --version && \
    openhands --version && \
    test -f ~/.claude.json && \
    test -f ~/.claude/settings.json && \
    test -f ~/.claude/CLAUDE.md && \
    git config --global user.name | grep -q ola
