"""Application configuration using Pydantic Settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    """Application settings."""

    # OpenAI API
    openai_api_key: str

    # Data directory
    db_query_data_dir: str = str(Path.home() / ".db_query")

    # Logging
    log_level: str = "INFO"

    # CORS
    cors_origins: str = "*"

    # Query configuration
    query_default_limit: int = 1000
    query_history_retention: int = 50

    # Database pool configuration
    db_pool_min_size: int = 1
    db_pool_max_size: int = 5
    db_pool_command_timeout: int = 60

    # Metadata cache configuration
    metadata_cache_hours: int = 24

    # Access control. Empty table/column lists mean no extra denylist.
    # Blocked functions always apply, and EXPLAIN stays off unless enabled.
    security_blocked_tables: str = ""
    security_blocked_columns: str = ""
    security_allow_explain: bool = False
    security_blocked_functions: str = (
        "pg_sleep,pg_terminate_backend,pg_cancel_backend,pg_read_file,"
        "pg_read_binary_file,pg_ls_dir,pg_stat_file,lo_import,lo_export,"
        "dblink,dblink_exec,pg_write_file,sleep"
    )

    # Resilience. These values are read by the request path, not only stored.
    rate_limit_query_concurrency: int = 10
    rate_limit_llm_concurrency: int = 5
    rate_limit_acquire_timeout: float = 2.0
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 0.05
    retry_max_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 5
    circuit_breaker_recovery_seconds: float = 30.0

    # Observability
    metrics_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins string into list."""
        if self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",")]

    @property
    def db_path(self) -> Path:
        """Get SQLite database path."""
        data_dir = Path(self.db_query_data_dir).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "db_query.db"

    def connection_config(self, url: str, name: str) -> "ConnectionConfig":
        """Build a pool config from the settings that used to go unread."""
        from app.adapters.base import ConnectionConfig

        return ConnectionConfig(
            url=url,
            name=name,
            min_pool_size=self.db_pool_min_size,
            max_pool_size=self.db_pool_max_size,
            command_timeout=self.db_pool_command_timeout,
        )


settings = Settings()
