from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    development = "development"
    test = "test"
    production = "production"


class Settings(BaseSettings):
    database_url: SecretStr = Field(validation_alias="DATABASE_URL")
    meta_access_token: SecretStr = Field(validation_alias="META_ACCESS_TOKEN")
    meta_phone_number_id: str = Field(validation_alias="META_PHONE_NUMBER_ID")
    meta_waba_id: str = Field(validation_alias="META_WABA_ID")
    meta_app_secret: SecretStr = Field(validation_alias="META_APP_SECRET")
    meta_verify_token: SecretStr = Field(validation_alias="META_VERIFY_TOKEN")
    environment: Environment = Field(validation_alias="ENVIRONMENT")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def ensure_async_postgresql_url(cls, value: Any) -> str:
        raw_value = (
            value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        )
        if raw_value.startswith("postgres://"):
            return raw_value.replace("postgres://", "postgresql+asyncpg://", 1)
        if raw_value.startswith("postgresql://"):
            return raw_value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not raw_value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        return raw_value


@lru_cache
def get_settings() -> Settings:
    return Settings()
