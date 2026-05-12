# Generic agent harness — base image
#
# Toolchain every project we've seen needs:
# - Node 22 LTS (also a hard dependency of the claude CLI install)
# - Python 3 + pip (for projects that use it)
# - git, gh CLI, jq (VCS + GitHub API + JSON wrangling)
# - claude CLI (the agent runtime; uses OAuth via env var)
#
# Project-level dependencies (npm install / pip install -r requirements.txt /
# go mod download / etc.) are NOT baked in. The orchestrator runs them
# per-container on startup so the image stays generic across projects.
# Add language-specific toolchains by extending this file in your project's
# fork or by maintaining a project-specific Dockerfile that FROMs this one.
#
# Patterns adopted from .sandcastle/Dockerfile:
#   - Rename the base image's `node` user (UID 1000) to `agent` instead of
#     creating a new one — keeps UID 1000 stable for bind-mount ownership.
#   - PIP_BREAK_SYSTEM_PACKAGES=1 so per-run pip install hooks don't fail
#     under Debian 12's PEP 668 lockdown.
#   - Install Claude CLI via the official install.sh — sets up ~/.claude
#     skeleton correctly and matches Anthropic's recommended install path.

FROM node:22-bookworm-slim

# System tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential jq \
    python3 python3-pip python3-venv \
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
CMD ["bash", "-lc", "node --version && python3 --version && git --version && gh --version | head -1 && claude --version"]
