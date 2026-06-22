"""
Application configuration — reads from environment variables.
Secrets are injected via ECS task environment (from Secrets Manager).
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Database ──────────────────────────────────────────────────────────────
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "miso_elt"
    db_user: str = "miso_app"
    db_password: str = ""

    # Read-only reporting user (API layer uses this)
    db_readonly_user: str = "miso_readonly"
    db_readonly_password: str = ""

    # ── MISO API ──────────────────────────────────────────────────────────────
    miso_api_url: str = "https://public-api.misoenergy.org/api/FuelMix"
    miso_poll_interval_seconds: int = 60   # enforced floor — never poll faster
    miso_request_timeout_seconds: int = 15
    miso_max_retries: int = 3

    # ── API server ────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_key: str = ""          # Bearer token for external consumers
    environment: str = "production"
    log_level: str = "INFO"

    # ── AWS / CloudWatch ──────────────────────────────────────────────────────
    aws_region: str = "us-east-1"
    cloudwatch_namespace: str = "MISO/ELT"
    sns_alert_topic_arn: str = ""

    @property
    def app_db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def readonly_db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_readonly_user}:{self.db_readonly_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
