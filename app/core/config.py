from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import re
from typing import Any
from urllib.parse import urlsplit

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


class CloudTasksConfigurationError(RuntimeError):
    """Erro seguro para configuração ausente do Cloud Tasks."""


@dataclass(frozen=True, slots=True)
class CloudTasksConfiguration:
    project_id: str
    region: str
    queue: str
    target_url: str
    oidc_audience: str
    invoker_email: str


class Settings(BaseSettings):
    app_commit_sha: str | None = Field(default=None, validation_alias="APP_COMMIT_SHA")
    diagnostics_oidc_audience: str | None = Field(
        default=None, validation_alias="DIAGNOSTICS_OIDC_AUDIENCE"
    )
    diagnostics_invoker_email: str | None = Field(
        default=None, validation_alias="DIAGNOSTICS_INVOKER_EMAIL"
    )
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
    gcp_project_id: str | None = Field(
        default=None, validation_alias="GCP_PROJECT_ID"
    )
    gcp_region: str | None = Field(default=None, validation_alias="GCP_REGION")
    cloud_tasks_events_queue: str | None = Field(
        default=None, validation_alias="CLOUD_TASKS_EVENTS_QUEUE"
    )
    cloud_tasks_target_url: str | None = Field(
        default=None, validation_alias="CLOUD_TASKS_TARGET_URL"
    )
    cloud_tasks_oidc_audience: str | None = Field(
        default=None, validation_alias="CLOUD_TASKS_OIDC_AUDIENCE"
    )
    cloud_tasks_invoker_email: str | None = Field(
        default=None, validation_alias="CLOUD_TASKS_INVOKER_EMAIL"
    )
    cloud_tasks_enabled: bool = Field(
        default=False, validation_alias="CLOUD_TASKS_ENABLED"
    )
    outbound_tasks_enabled: bool = Field(
        default=False, validation_alias="OUTBOUND_TASKS_ENABLED"
    )
    cloud_tasks_outbound_queue: str = Field(
        default="whatsapp-outbound",
        validation_alias="CLOUD_TASKS_OUTBOUND_QUEUE",
    )
    cloud_tasks_outbound_target_url: str | None = Field(
        default=None,
        validation_alias="CLOUD_TASKS_OUTBOUND_TARGET_URL",
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

    @field_validator("app_commit_sha", mode="before")
    @classmethod
    def safe_commit_sha(cls, value: Any) -> str | None:
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
            return value.lower()
        return None

    @field_validator("diagnostics_oidc_audience", "diagnostics_invoker_email", mode="before")
    @classmethod
    def empty_diagnostics_identity(cls, value: Any) -> Any:
        # Empty entries from .env.example must not disable the existing identity.
        return None if isinstance(value, str) and not value.strip() else value

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

    def require_cloud_tasks_configuration(self) -> CloudTasksConfiguration:
        return self._require_cloud_tasks_configuration(
            enabled=self.cloud_tasks_enabled,
            queue=self.cloud_tasks_events_queue,
            target_url=self.cloud_tasks_target_url,
            disabled_message="Cloud Tasks is disabled",
        )

    def require_outbound_tasks_configuration(self) -> CloudTasksConfiguration:
        return self._require_cloud_tasks_configuration(
            enabled=self.outbound_tasks_enabled,
            queue=self.cloud_tasks_outbound_queue,
            target_url=self.cloud_tasks_outbound_target_url,
            disabled_message="Outbound tasks are disabled",
        )

    def _require_cloud_tasks_configuration(
        self,
        *,
        enabled: bool,
        queue: str | None,
        target_url: str | None,
        disabled_message: str,
    ) -> CloudTasksConfiguration:
        if not enabled:
            raise CloudTasksConfigurationError(disabled_message)

        raw_values = {
            "project_id": self.gcp_project_id,
            "region": self.gcp_region,
            "queue": queue,
            "target_url": target_url,
            "oidc_audience": self.cloud_tasks_oidc_audience,
            "invoker_email": self.cloud_tasks_invoker_email,
        }
        values = {
            key: value.strip() if isinstance(value, str) else ""
            for key, value in raw_values.items()
        }
        if any(not value for value in values.values()):
            raise CloudTasksConfigurationError(
                "Cloud Tasks configuration is incomplete"
            )
        target_url = urlsplit(values["target_url"])
        oidc_audience = urlsplit(values["oidc_audience"])
        if (
            target_url.scheme != "https"
            or not target_url.netloc
            or oidc_audience.scheme != "https"
            or not oidc_audience.netloc
            or "@" not in values["invoker_email"]
        ):
            raise CloudTasksConfigurationError(
                "Cloud Tasks configuration is invalid"
            )
        return CloudTasksConfiguration(**values)

    @staticmethod
    def _require_meta_secret(value: SecretStr | None) -> str:
        if value is None or not value.get_secret_value().strip():
            raise MetaConfigurationError("WhatsApp integration is not configured")
        return value.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
