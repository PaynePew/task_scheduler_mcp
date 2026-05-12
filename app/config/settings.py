from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://app:app@postgres:5432/app"
    alembic_database_url: str = "postgresql+psycopg://app:app@postgres:5432/app"
    queue_url: str = "http://elasticmq:9324/queue/task-queue"
    mcp_user_id: str = "default-user"
    port: int = 8000
    log_level: str = "INFO"

    # Per-process DB pool defaults per ADR-011 (mcp-server / worker profile)
    db_pool_size: int = 5
    db_max_overflow: int = 10


settings = Settings()
