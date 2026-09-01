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


class DatabaseConfigurationError(RuntimeError):
    """Erro seguro para configuração ausente do banco de dados."""


class MetaConfigurationError(RuntimeError):
    """Erro seguro para configuração ausente da integração Meta."""


class Settings(BaseSettings):
    database_url: SecretStr | None = Field(
        default=None, validation_alias="DATABASE_URL"
    )
    alembic_database_url: SecretStr | None = Field(
        default=None, validation_alias="ALEMBIC_DATABASE_URL"
    )
    meta_access_token: SecretStr | None = Field(
        default=None, validation_alias="META_ACCESS_TOKEN"
    )
    meta_phone_number_id: str | None = Field(
        default=None, validation_alias="META_PHONE_NUMBER_ID"
    )
    meta_graph_version: str | None = Field(
        default=None,
        validation_alias="META_GRAPH_VERSION",
    )
    meta_waba_id: str | None = Field(
        default=None, validation_alias="META_WABA_ID"
    )
    meta_app_secret: SecretStr | None = Field(
        default=None, validation_alias="META_APP_SECRET"
    )
    meta_verify_token: SecretStr | None = Field(
        default=None, validation_alias="META_VERIFY_TOKEN"
    )
    environment: Environment = Field(validation_alias="ENVIRONMENT")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    @field_validator("database_url", "alembic_database_url", mode="before")
    @classmethod
    def ensure_async_postgresql_url(cls, value: Any) -> str | None:
        if value is None:
            return None

        raw_value = (
            value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        ).strip()
        if not raw_value:
            return None
        if raw_value.startswith("postgres://"):
            return raw_value.replace("postgres://", "postgresql+asyncpg://", 1)
        if raw_value.startswith("postgresql://"):
            return raw_value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if not raw_value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        return raw_value

    def require_database_url(self) -> str:
        if self.database_url is None:
            raise DatabaseConfigurationError(
                "DATABASE_URL is required to use the database"
            )
        return self.database_url.get_secret_value()

    def require_alembic_database_url(self) -> str:
        if self.alembic_database_url is not None:
            return self.alembic_database_url.get_secret_value()
        return self.require_database_url()

    def require_meta_app_secret(self) -> str:
        return self._require_meta_secret(self.meta_app_secret)

    def require_meta_verify_token(self) -> str:
        return self._require_meta_secret(self.meta_verify_token)

    @staticmethod
    def _require_meta_secret(value: SecretStr | None) -> str:
        if value is None or not value.get_secret_value().strip():
            raise MetaConfigurationError("WhatsApp integration is not configured")
        return value.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
