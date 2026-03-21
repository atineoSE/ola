FROM docker/sandbox-templates:shell

# Install uv (Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Copy project and install ola (brings in openhands-sdk as a dependency)
COPY . /tmp/ola-src
RUN /root/.local/bin/uv tool install /tmp/ola-src && rm -rf /tmp/ola-src
