from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Bot
    bot_token: str = Field(..., validation_alias="BOT_TOKEN")

    # Database
    database_url: str = Field(..., validation_alias="DATABASE_URL")
    db_pool_min: int = Field(10, validation_alias="DB_POOL_MIN")
    db_pool_max: int = Field(20, validation_alias="DB_POOL_MAX")

    # Dig settings
    dig_cooldown_seconds: int = Field(3600, validation_alias="DIG_COOLDOWN_SECONDS")
    dig_min_kg: float = Field(1.0, validation_alias="DIG_MIN_KG")
    dig_max_kg: float = Field(7.0, validation_alias="DIG_MAX_KG")
    dig_precision: int = Field(1, validation_alias="DIG_PRECISION")

    # History
    history_retention_hours: int = Field(48, validation_alias="HISTORY_RETENTION_HOURS")
    cleanup_interval_seconds: int = Field(3600, validation_alias="CLEANUP_INTERVAL_SECONDS")

    # Rate limiting
    rate_limit_requests: int = Field(30, validation_alias="RATE_LIMIT_REQUESTS")
    rate_limit_window: int = Field(60, validation_alias="RATE_LIMIT_WINDOW")

    # Web server
    web_host: str = Field("0.0.0.0", validation_alias="WEB_HOST")
    web_port: int = Field(8080, validation_alias="WEB_PORT")


    # Logging
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")
    log_format: str = Field("json", validation_alias="LOG_FORMAT")


    # Sentry
    sentry_dsn: str | None = Field(None, validation_alias="SENTRY_DSN")
    sentry_traces_sample_rate: float = Field(0.1, validation_alias="SENTRY_TRACES_SAMPLE_RATE")
    sentry_profiles_sample_rate: float = Field(0.1, validation_alias="SENTRY_PROFILES_SAMPLE_RATE")

    # Admin (Telegram-only panel: gate for every admin action)
    admin_user_ids: str = Field("", validation_alias="ADMIN_USER_IDS")  # comma-separated

    # Daily bonus
    daily_bonus_enabled: bool = Field(True, validation_alias="DAILY_BONUS_ENABLED")
    daily_bonus_base_kg: float = Field(0.5, validation_alias="DAILY_BONUS_BASE_KG")
    daily_bonus_streak_multiplier: float = Field(0.1, validation_alias="DAILY_BONUS_STREAK_MULTIPLIER")
    daily_bonus_max_streak: int = Field(30, validation_alias="DAILY_BONUS_MAX_STREAK")

    # Achievements
    achievements_enabled: bool = Field(True, validation_alias="ACHIEVEMENTS_ENABLED")

    # Anti-cheat
    anticheat_enabled: bool = Field(True, validation_alias="ANTICHEAT_ENABLED")
    anticheat_max_kg_per_hour: float = Field(50.0, validation_alias="ANTICHEAT_MAX_KG_PER_HOUR")
    anticheat_min_dig_interval: float = Field(0.5, validation_alias="ANTICHEAT_MIN_DIG_INTERVAL")

    @field_validator("bot_token")
    @classmethod
    def validate_bot_token(cls, v: str) -> str:
        if not v or v.count(":") != 1:
            raise ValueError("Invalid bot token format")
        return v

    @field_validator("database_url")
    @classmethod
    def validate_db_url(cls, v: str) -> str:
        if not v.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be postgresql://...")
        if "sslmode=" in v:
            return v
        # Only managed (public) hosts require TLS. Local / docker-network
        # hosts (no dot in hostname, or loopback) must stay plain.
        host = v.split("://", 1)[-1].split("@")[-1].split("/")[0].split(":")[0]
        local = host in ("localhost", "127.0.0.1", "::1", "db", "postgres", "pg")
        if "." not in host and not local:
            return v
        if local:
            return v
        return v + ("&" if "?" in v else "?") + "sslmode=require"

    @property
    def admin_ids(self) -> list[int]:
        if not self.admin_user_ids:
            return []
        return [int(x.strip()) for x in self.admin_user_ids.split(",") if x.strip().isdigit()]


@lru_cache
def get_settings() -> Settings:
    return Settings()