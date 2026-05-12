# Project-customised agent harness — chatgpt_task
#
# Forked from kanban-harness v0.1.1 (upstream sha 90fb5fb).
# Project-specific additions beyond the generic base:
#   - `uv` binary copied from astral-sh image (matches the runtime Dockerfile
#     at repo root; required because prompts/implement.md tells the agent to
#     run `uv run pytest`).
#   - `postgresql-client` for ad-hoc DB introspection during integration tests.
#
# Why uv is baked into the image and not installed per-run:
#   Earlier slices (issue #10) had the agent hit `uv: command not found`,
#   fall back to `pip3 install pytest`, and skip integration tests entirely.
#   That mode let SQLAlchemy-session / migration / queue bugs slip through to
#   GitHub Actions CI. Baking uv in lets the agent honour the prompt's
#   "run integration before claiming COMPLETE" instruction.

FROM node:22-bookworm-slim

# System tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential jq \
    python3 python3-pip python3-venv \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Allow pip to install into system Python inside the container
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# GitHub CLI (Debian repo)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | tee /usr/share/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# uv — pinned to match the project runtime Dockerfile at repo root.
# Bump deliberately when the runtime image is bumped.
COPY --from=ghcr.io/astral-sh/uv:0.5.20 /uv /uvx /usr/local/bin/

# Rename the base image's `node` user (UID 1000) to `agent` and move its
# home dir. Avoids a UID collision that would happen if we created a new
# user with -u 1000.
RUN usermod -d /home/agent -m -l agent node \
    && groupmod -n agent node

USER agent

# Claude Code CLI — installed per-user via Anthropic's official script.
# Uses subscription via OAuth (NOT API key). Goes to ~/.local/bin.
RUN curl -fsSL https://claude.ai/install.sh | bash

# Add the per-user install dir to PATH
ENV PATH="/home/agent/.local/bin:$PATH"

WORKDIR /workspace

# Sanity: print versions on container start unless overridden
CMD ["bash", "-lc", "node --version && python3 --version && uv --version && git --version && gh --version | head -1 && claude --version"]
