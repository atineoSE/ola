FROM docker/sandbox-templates:shell

# Install uv (Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code


# Install OpenHands CLI
RUN $HOME/.local/bin/uv tool install openhands --python 3.12

# Copy project and install ola (brings in openhands-sdk as a dependency)
COPY --chown=1000:1000 . /tmp/ola-src
RUN $HOME/.local/bin/uv tool install /tmp/ola-src && rm -rf /tmp/ola-src

# Skip onboarding and bypass permissions
RUN mkdir -p ~/.claude && \
    echo '{"hasCompletedOnboarding":true}' > ~/.claude.json && \
    echo '{"permissions":{"defaultMode":"bypassPermissions"}}' > ~/.claude/settings.json

# On shell login: unset proxy-managed API key, install Claude credentials from workspace
RUN echo 'unset ANTHROPIC_API_KEY' >> ~/.bashrc && \
    echo 'if [ -f .credentials.json ]; then mv .credentials.json ~/.claude/.credentials.json && chmod 600 ~/.claude/.credentials.json; fi' >> ~/.bashrc && \
    echo "alias claude-yolo='claude --dangerously-skip-permissions'" >> ~/.bashrc && \
    echo "alias oh='openhands --llm-approve --exit-without-confirmation'" >> ~/.bashrc
