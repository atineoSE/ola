FROM docker/sandbox-templates:shell

# Install uv (Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Clear proxy-managed API key from base image so Claude Code uses OAuth credentials
ENV ANTHROPIC_API_KEY=

# Copy project and install ola (brings in openhands-sdk as a dependency)
COPY --chown=1000:1000 . /tmp/ola-src
RUN $HOME/.local/bin/uv tool install /tmp/ola-src && rm -rf /tmp/ola-src

# Auto-install and remove Claude credentials from workspace on shell login
RUN echo 'if [ -f .credentials.json ]; then mkdir -p ~/.claude && mv .credentials.json ~/.claude/.credentials.json && chmod 600 ~/.claude/.credentials.json; fi' >> ~/.bashrc
