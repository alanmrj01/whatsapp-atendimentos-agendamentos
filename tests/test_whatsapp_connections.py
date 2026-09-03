from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.whatsapp.client import (
    WhatsAppClientConfiguration,
    WhatsAppConfigurationError,
)
from app.whatsapp.connections import (
    WhatsAppConnectionMode,
    WhatsAppConnectionRecord,
    WhatsAppConnectionStatus,
    WhatsAppProvider,
    sanitize_error_code,
    validate_credential_secret_ref,
)
from app.whatsapp.sender import BusinessWhatsAppSenderResolver

BUSINESS_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BUSINESS_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def connection(
    business_id: uuid.UUID,
    phone_number_id: str | None,
    *,
    status: WhatsAppConnectionStatus = WhatsAppConnectionStatus.CONNECTED,
    credential_secret_ref: str | None = None,
    graph_version: str | None = "v23.0",
) -> WhatsAppConnectionRecord:
    return WhatsAppConnectionRecord(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"connection:{business_id}"),
        business_id=business_id,
        provider=WhatsAppProvider.META,
        mode=WhatsAppConnectionMode.API_ONLY,
        status=status,
        meta_waba_id=f"waba-{business_id.hex[:8]}",
        meta_phone_number_id=phone_number_id,
        credential_secret_ref=credential_secret_ref,
        graph_version=graph_version,
        connected_at=datetime.now(timezone.utc),
    )


class FakeConnectionRepository:
    def __init__(
        self,
        connections: dict[uuid.UUID, WhatsAppConnectionRecord | None],
        legacy_phones: dict[uuid.UUID, str | None] | None = None,
    ) -> None:
        self.connections = connections
        self.legacy_phones = legacy_phones or {}
        self.lookups: list[uuid.UUID] = []

    async def get_connection_record(
        self, business_id: uuid.UUID
    ) -> WhatsAppConnectionRecord | None:
        self.lookups.append(business_id)
        return self.connections.get(business_id)

    async def get_legacy_phone_number_id(
        self, business_id: uuid.UUID
    ) -> str | None:
        return self.legacy_phones.get(business_id)


class MappingCredentialProvider:
    def __init__(self, values: dict[uuid.UUID, str]) -> None:
        self.values = values

    async def resolve(
        self, record: WhatsAppConnectionRecord
    ) -> SecretStr:
        value = self.values.get(record.business_id)
        if value is None:
            raise WhatsAppConfigurationError(
                "WhatsApp credential is not configured"
            )
        return SecretStr(value)


class FakeSender:
    async def aclose(self) -> None:
        return None


def settings() -> Settings:
    return Settings(_env_file=None).model_copy(
        update={
            "meta_access_token": SecretStr("legacy-credential"),
            "meta_phone_number_id": "legacy-phone",
            "meta_graph_version": "v23.0",
        }
    )


@pytest.mark.asyncio
async def test_two_businesses_resolve_distinct_client_configurations() -> None:
    repository = FakeConnectionRepository(
        {
            BUSINESS_A: connection(BUSINESS_A, "phone-a"),
            BUSINESS_B: connection(BUSINESS_B, "phone-b"),
        }
    )
    captured: list[WhatsAppClientConfiguration] = []

    def build(configuration: WhatsAppClientConfiguration) -> FakeSender:
        captured.append(configuration)
        return FakeSender()

    resolver = BusinessWhatsAppSenderResolver(
        repository,
        settings(),
        credential_provider=MappingCredentialProvider(
            {BUSINESS_A: "credential-a", BUSINESS_B: "credential-b"}
        ),
        client_builder=build,
    )

    await resolver.resolve(BUSINESS_A)
    await resolver.resolve(BUSINESS_B)

    assert [item.phone_number_id for item in captured] == ["phone-a", "phone-b"]
    assert [item.access_token.get_secret_value() for item in captured] == [
        "credential-a",
        "credential-b",
    ]
    assert repository.lookups == [BUSINESS_A, BUSINESS_B]


@pytest.mark.parametrize(
    "status",
    [
        WhatsAppConnectionStatus.PENDING,
        WhatsAppConnectionStatus.DISCONNECTED,
        WhatsAppConnectionStatus.ERROR,
    ],
)
@pytest.mark.asyncio
async def test_non_connected_connection_fails_closed(
    status: WhatsAppConnectionStatus,
) -> None:
    resolver = BusinessWhatsAppSenderResolver(
        FakeConnectionRepository(
            {BUSINESS_A: connection(BUSINESS_A, "phone-a", status=status)}
        ),
        settings(),
        credential_provider=MappingCredentialProvider(
            {BUSINESS_A: "credential-a"}
        ),
    )

    with pytest.raises(WhatsAppConfigurationError):
        await resolver.resolve(BUSINESS_A)


@pytest.mark.asyncio
async def test_missing_phone_or_credential_fails_closed() -> None:
    missing_phone = BusinessWhatsAppSenderResolver(
        FakeConnectionRepository({BUSINESS_A: connection(BUSINESS_A, None)}),
        settings(),
        credential_provider=MappingCredentialProvider(
            {BUSINESS_A: "credential-a"}
        ),
    )
    missing_credential = BusinessWhatsAppSenderResolver(
        FakeConnectionRepository(
            {BUSINESS_A: connection(BUSINESS_A, "phone-a")}
        ),
        settings(),
        credential_provider=MappingCredentialProvider({}),
    )

    with pytest.raises(WhatsAppConfigurationError):
        await missing_phone.resolve(BUSINESS_A)
    with pytest.raises(WhatsAppConfigurationError):
        await missing_credential.resolve(BUSINESS_A)


@pytest.mark.asyncio
async def test_cross_business_connection_is_rejected() -> None:
    resolver = BusinessWhatsAppSenderResolver(
        FakeConnectionRepository(
            {BUSINESS_A: connection(BUSINESS_B, "phone-b")}
        ),
        settings(),
        credential_provider=MappingCredentialProvider(
            {BUSINESS_B: "credential-b"}
        ),
    )

    with pytest.raises(WhatsAppConfigurationError):
        await resolver.resolve(BUSINESS_A)


@pytest.mark.asyncio
async def test_legacy_pilot_fallback_requires_exact_business_phone_mapping() -> None:
    captured: list[WhatsAppClientConfiguration] = []
    repository = FakeConnectionRepository(
        {BUSINESS_A: None, BUSINESS_B: None},
        {BUSINESS_A: "legacy-phone", BUSINESS_B: "different-phone"},
    )
    resolver = BusinessWhatsAppSenderResolver(
        repository,
        settings(),
        client_builder=lambda configuration: (
            captured.append(configuration) or FakeSender()
        ),
    )

    await resolver.resolve(BUSINESS_A)
    with pytest.raises(WhatsAppConfigurationError):
        await resolver.resolve(BUSINESS_B)

    assert [item.phone_number_id for item in captured] == ["legacy-phone"]


@pytest.mark.asyncio
async def test_global_provider_does_not_resolve_secret_reference() -> None:
    record = connection(
        BUSINESS_A,
        "legacy-phone",
        credential_secret_ref=(
            "projects/project-a/secrets/whatsapp-token/versions/latest"
        ),
    )
    resolver = BusinessWhatsAppSenderResolver(
        FakeConnectionRepository({BUSINESS_A: record}),
        settings(),
    )

    with pytest.raises(WhatsAppConfigurationError):
        await resolver.resolve(BUSINESS_A)


def test_secret_references_and_error_codes_are_sanitized() -> None:
    reference = "projects/project-a/secrets/whatsapp-token/versions/latest"

    assert validate_credential_secret_ref(reference) == reference
    with pytest.raises(ValueError):
        validate_credential_secret_ref("plain-text-token")
    assert sanitize_error_code("oauth_permission_error") == (
        "oauth_permission_error"
    )
    assert sanitize_error_code("remote payload: secret value") == (
        "provider_error"
    )


@pytest.mark.asyncio
async def test_sensitive_connection_values_are_absent_from_logs_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_phone = "5511999999999"
    sensitive_token = "sensitive-token-value"
    caplog.set_level(logging.DEBUG)
    resolver = BusinessWhatsAppSenderResolver(
        FakeConnectionRepository(
            {BUSINESS_A: connection(BUSINESS_A, sensitive_phone)}
        ),
        settings().model_copy(
            update={
                "meta_access_token": SecretStr(sensitive_token),
                "meta_phone_number_id": "another-phone",
            }
        ),
    )

    with pytest.raises(WhatsAppConfigurationError) as exc_info:
        await resolver.resolve(BUSINESS_A)

    output = caplog.text + str(exc_info.value)
    assert sensitive_phone not in output
    assert sensitive_token not in output
