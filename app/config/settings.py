"""App-wide settings loaded from environment (and ``.env`` if present).

Single source of truth for env-var names per ADR-010. Add new envs here rather
than reading ``os.environ`` directly elsewhere — the type-checked values surface
in autocomplete and missing required vars fail fast at import.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Two URLs by design (ADR-011):
    #   - database_url       → async runtime (asyncpg). Used by every service.
    #   - alembic_database_url → sync (psycopg). Alembic + async is rough-edged;
    #                            migrations use the sync driver for stability.
    # Both must point at the same physical database.
    #
    # Defaults use `localhost` so host-side entrypoints (stdio MCP, `uv run alembic`,
    # `uv run pytest`) work even without a `.env` file. In-container services
    # override these via `env_file: .env.docker` in docker-compose.yml, which uses
    # the compose-internal hostnames (`postgres`, `elasticmq`).
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    alembic_database_url: str = "postgresql+psycopg://app:app@localhost:5432/app"
    # ElasticMQ locally; in W3 this becomes the real SQS URL via env override.
    queue_url: str = "http://localhost:9324/queue/task-queue"
    queue_dlq_url: str = "http://localhost:9324/queue/task-dlq"
    # Default identity when stdio transport runs without MCP_USER_ID set (ADR-015).
    mcp_user_id: str = "default-user"
    # Optional user timezone forwarded from the client environment (ADR-017).
    # Falls back to the X-Timezone header, then "UTC". Set via MCP_USER_TZ.
    mcp_user_tz: str | None = None
    port: int = 8000
    log_level: str = "INFO"
    # Short git SHA stamped in by the Docker build; surfaced via /healthz so
    # ALB / Caddy / Better Stack can correlate a running container with a build.
    git_sha: str = "unknown"

    # Default pool size is the mcp-server / worker profile (ADR-011).
    # The watcher / recurring / chain entrypoints override these to 2+3 because
    # they run a single tight loop and don't need ten connections each. Sized
    # so the W3 footprint fits inside RDS db.t4g.micro's max_connections=81.
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # Reconciler grace windows (issue #30).
    # DLQ grace: VisibilityTimeout(30s) * (MaxReceiveCount(3) + 1) + slack(60s) = 180s.
    # QUEUED grace: VisibilityTimeout(30s) * 2 + slack(30s) = 90s.
    reconciler_dlq_grace_seconds: int = 180
    reconciler_queued_grace_seconds: int = 90

    # Per-user rate limits for task.create.v1 (ADR-042).
    # Two windows: a 24h daily cap and a 1-minute burst cap.
    rate_limit_daily: int = 1000
    rate_limit_burst_per_minute: int = 10

    # Overload protection (ADR-057).
    # CPU / RAM thresholds: fractions in [0, 1]; 0.90 = 90%.
    overload_cpu_threshold: float = 0.90
    overload_ram_threshold: float = 0.90
    # Queue depth thresholds (absolute message count).
    # overload_queue_depth_threshold: triggers load shedding (503).
    # overload_backpressure_queue_depth: task.create returns 429.
    overload_queue_depth_threshold: int = 1000
    overload_backpressure_queue_depth: int = 500
    # Max concurrent in-flight MCP requests before 503 is returned.
    overload_concurrency_limit: int = 10
    # Retry-After header value (seconds) sent with 503/429 overload responses.
    overload_retry_after_seconds: int = 10


settings = Settings()
