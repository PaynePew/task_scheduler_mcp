FROM python:3.12-slim

WORKDIR /app

# Install uv (pin to a specific release for reproducible builds; bump deliberately)
COPY --from=ghcr.io/astral-sh/uv:0.5.20 /uv /uvx /usr/local/bin/

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install production dependencies (no dev extras)
RUN uv sync --frozen --no-dev

# Copy application source
COPY app/ ./app/

# uv-managed venv is at /app/.venv; add it to PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

# Default entrypoint — override via `command:` in Compose or ECS task definition
CMD ["python", "-m", "app.entrypoints.mcp_stdio"]
