from __future__ import annotations

from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.auth.schemas import StrictRequest
from app.auth.security import normalize_email


class PlatformBusinessCreateRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=255)
    timezone: str = Field(default="America/Sao_Paulo", min_length=1, max_length=64)
    owner_email: str = Field(max_length=254)
    owner_password: SecretStr = Field(min_length=12, max_length=1024)

    @field_validator("name")
    @classmethod
    def normalized_name(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if not value:
            raise ValueError("Business name is required")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid timezone") from None
        return value

    @field_validator("owner_email")
    @classmethod
    def normalized_owner_email(cls, value: str) -> str:
        return normalize_email(value)


class PlatformBusinessStatusRequest(StrictRequest):
    active: bool = Field(strict=True)


class PlatformBusinessResponse(BaseModel):
    id: UUID
    name: str
    timezone: str
    active: bool
    owners: list[str]
    whatsapp_status: Literal["disconnected", "pending", "connected", "error"]


class PlatformBusinessListResponse(BaseModel):
    businesses: list[PlatformBusinessResponse]


class PlatformBusinessStatusResponse(BaseModel):
    id: UUID
    active: bool
