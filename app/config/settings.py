"""App-wide settings loaded from environment (and ``.env`` if present).

Single source of truth for env-var names per ADR-010. Add new envs here rather
than reading ``os.environ`` directly elsewhere — the type-checked values surface
in autocomplete and missing required vars fail fast at import.
"""

from pydantic import model_validator
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

    # Per-user rate limits for task.create.v1 (ADR-042, revised by ADR-055).
    # Two windows: a 24h daily cap and a 1-minute burst cap.
    # Defaults reduced from 1000/day, 10/min for multi-tenant safety (ADR-055).
    rate_limit_daily: int = 100
    rate_limit_burst_per_minute: int = 5

    # Containment caps at task.create (ADR-055).
    # Active-recurring per user: bounds permanent steady-state load.
    # Active-total per user: prevents one user hoarding the box.
    # Global-active-recurring ceiling: protects the single core.
    quota_active_recurring_per_user: int = 5
    quota_active_total_per_user: int = 50
    quota_global_active_recurring: int = 500

    # WorkOS AuthKit / OAuth 2.1 resource-server settings (ADR-053).
    # All three must be set together for HTTP auth to be enforced; when any
    # is absent the server runs in trust-only mode (for local dev / CI without
    # WorkOS credentials).  Set via environment variables:
    #   WORKOS_ISSUER       — e.g. "https://api.workos.com"
    #   WORKOS_JWKS_URI     — e.g. "https://api.workos.com/sso/jwks/<client_id>"
    #   WORKOS_AUDIENCE     — resource identifier bound to our server (RFC 8707)
    workos_issuer: str | None = None
    workos_jwks_uri: str | None = None
    workos_audience: str | None = None

    @model_validator(mode="after")
    def _validate_workos_all_or_nothing(self) -> "Settings":
        """Enforce that WorkOS vars are either all set or all unset.

        Partial config (1 or 2 of 3 set) silently degrades to trust-only mode,
        which accepts any spoofed X-User-Id header. Fail fast instead.
        """
        workos_vars = (
            ("WORKOS_JWKS_URI", self.workos_jwks_uri),
            ("WORKOS_ISSUER", self.workos_issuer),
            ("WORKOS_AUDIENCE", self.workos_audience),
        )
        missing = [name for name, val in workos_vars if val is None]
        # All set (missing=[]) and all unset (missing=all of them) are both legal.
        if missing and len(missing) < len(workos_vars):
            raise ValueError(
                f"Partial WorkOS configuration detected. "
                f"Missing: {', '.join(missing)}. "
                f"Either set all three (WORKOS_JWKS_URI, WORKOS_ISSUER, WORKOS_AUDIENCE) "
                f"for auth-enabled mode, or leave all three unset for trust-only dev mode."
            )
        return self

    # Operator user identity — the operator's WorkOS sub. When set:
    #   - exempt from rate-limit and containment caps (ADR-055), and
    #   - allowed to invoke requires_operator actions (ADR-053 action-tiering).
    # Leave unset (None) to disable both gates. Override via OPERATOR_USER_ID.
    #
    # Note: migration 0005 (ADR-059) reads OPERATOR_USER_ID directly from the
    # environment, not from this setting — keep both in sync at deploy time.
    operator_user_id: str | None = None

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

    # Better Stack (Logtail) log ingestion token (ADR-056).
    # When set, a LogtailHandler ships logs to Better Stack.
    # Leave unset in local dev — logs go to stdout only.
    better_stack_source_token: str | None = None

    # AWS KMS envelope encryption for OAuth token storage (ADR-054).
    # kms_key_id: CMK ARN or alias — required for connection-store encrypt/decrypt.
    # kms_region: AWS region where the CMK lives (Lightsail is ap-northeast-1).
    # aws_access_key_id / aws_secret_access_key: IAM user scoped to
    #   kms:GenerateDataKey + kms:Decrypt on this one CMK (stored in .env 0600).
    # All four are optional so local dev / CI without real KMS keeps working;
    # connection-store operations raise at runtime if kms_key_id is unset.
    kms_key_id: str | None = None
    kms_region: str = "ap-northeast-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    # Refresh window: re-fetch a connection's access token when it expires within
    # this many seconds (default 5 minutes).
    connection_refresh_window_seconds: int = 300

    # Operator-subsidized LLM actions (ADR-052).
    # LLM_MODEL: pinned cheap model — callers cannot override this.
    # LLM_API_KEY: operator's LLM provider API key (never user-supplied).
    # LLM_API_BASE_URL: OpenAI-compatible endpoint base URL.
    # LLM_MAX_OUTPUT_TOKENS: max_tokens sent to the provider on every call.
    # LLM_MAX_INPUT_CHARS: character limit on user/fetched content; handler
    #   truncates to this length before the provider call (no spend wasted).
    #   ~16k chars ≈ 4k tokens at ~4 chars/token for Latin text.
    # LLM_DAILY_TOKEN_BUDGET_PER_USER: per-user daily cap (in+out tokens).
    #   Exhausted → typed error before the call; resets at UTC midnight.
    # LLM_GLOBAL_MONTHLY_TOKEN_CEILING: optional app-side kill-switch (None
    #   disables the app-side check; provider dashboard hard stop is always
    #   the last-resort ceiling).
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str | None = None
    llm_api_base_url: str = "https://api.openai.com/v1"
    llm_max_output_tokens: int = 1024
    llm_max_input_chars: int = 16000
    llm_daily_token_budget_per_user: int = 10000
    llm_global_monthly_token_ceiling: int | None = None

    # WorkOS web OAuth client credentials (ADR-058, ADR-053).
    # Required for the /connections web dashboard login flow.
    # When absent, the web session uses trust-only mode (MCP_USER_ID / default-user).
    workos_client_id: str | None = None
    workos_client_secret: str | None = None

    # GitHub OAuth app credentials (ADR-058).
    # Required for the GitHub "Connect" flow on the /connections dashboard.
    github_client_id: str | None = None
    github_client_secret: str | None = None

    # Slack OAuth app credentials (ADR-058, issue #139).
    # Required for the Slack "Connect" flow on the /connections dashboard.
    slack_client_id: str | None = None
    slack_client_secret: str | None = None

    # Google OAuth app credentials (ADR-058).
    # Required for the Google "Connect" flow (Gmail send scope) on /connections.
    google_client_id: str | None = None
    google_client_secret: str | None = None

    # Base URL of this server — used to construct OAuth redirect URIs and the
    # connect_url hint in task.create error envelopes (ADR-058).
    # E.g. "https://scheduler.paynepew.dev" in production, "http://localhost:8000" locally.
    connections_base_url: str = "http://localhost:8000"

    # Secret key for signing the web session JWT cookie (ADR-058).
    # Must be a strong random string in production.
    web_session_secret: str = "dev-session-secret-change-in-production"


settings = Settings()
