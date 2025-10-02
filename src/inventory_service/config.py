"""Application configuration objects."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseSettings, Field, validator


class Settings(BaseSettings):
    """Pydantic settings used to configure the application."""

    app_name: str = Field(
        default="Starbrew Inventory Service",
        description="Human friendly name for the API.",
    )
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Deployment environment flag used for logging/metrics.",
    )
    database_url: str = Field(
        default="sqlite+aiosqlite:///./inventory.db",
        description="SQLAlchemy compatible database URL.",
    )
    echo_sql: bool = Field(
        default=False,
        description="Enable SQL echo logging for debugging.",
    )
    access_control_allow_origin: str = Field(
        default="*",
        description="Allowed CORS origins for the API.",
    )
    default_timezone: str = Field(
        default="UTC",
        description="Timezone used for date/time normalization.",
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @validator("database_url")
    def _validate_sqlite_path(cls, value: str) -> str:
        if value.startswith("sqlite") and ":memory:" not in value and "///" not in value:
            raise ValueError(
                "SQLite database URLs should be in the form sqlite+aiosqlite:///path/to/db"
            )
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached instance of :class:`Settings`."""

    return Settings()


__all__ = ["Settings", "get_settings"]
