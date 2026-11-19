from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from mcp import Client
from mcp.server.auth.provider import AccessToken

import app.mcp_server.v2 as mcp_v2
from app.mcp_server.runtime import MCPConfigurationError
from app.mcp_server.v2 import build_mcp_v2_runtime


RESOURCE = "https://mcp.example.com/mcp-v2"
ISSUER = "https://auth.example.com"
BUSINESS_ID = UUID(
    "11111111-1111-4111-8111-111111111111"
)
METADATA = (
    "https://mcp.example.com/"
    ".well-known/oauth-protected-resource/mcp-v2"
)

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {
            "name": "alovia-mcp-v2-test",
            "version": "1.0",
        },
    },
}


class StaticTokenVerifier:
    async def verify_token(
        self,
        token: str,
    ) -> AccessToken | None:
        tokens = {
            "valid": AccessToken(
                token="valid",
                client_id="chatgpt-test",
                scopes=["alovia.read"],
                resource=RESOURCE,
                subject="user-test",
                claims={
                    "iss": ISSUER,
                    "business_id": str(BUSINESS_ID),
                },
            ),
            "wrong-scope": AccessToken(
                token="wrong-scope",
                client_id="chatgpt-test",
                scopes=["other.scope"],
                resource=RESOURCE,
                subject="user-test",
                claims={"iss": ISSUER},
            ),
            "wrong-resource": AccessToken(
                token="wrong-resource",
                client_id="chatgpt-test",
                scopes=["alovia.read"],
                resource="https://other.example.com/mcp",
                subject="user-test",
                claims={"iss": ISSUER},
            ),
        }
        return tokens.get(token)


def _runtime(profile_loader=None):
    return build_mcp_v2_runtime(
        allowed_hosts=("mcp.example.com",),
        issuer_url=ISSUER,
        resource_server_url=RESOURCE,
        token_verifier=StaticTokenVerifier(),
        profile_loader=profile_loader,
    )


@pytest.mark.asyncio
async def test_v2_publishes_authenticated_profile_tool() -> None:
    runtime = _runtime()

    async with Client(
        runtime.server,
        raise_exceptions=True,
    ) as client:
        tools = await client.list_tools()

    assert len(tools.tools) == 1

    descriptor = tools.tools[0].model_dump(
        by_alias=True
    )

    assert descriptor["name"] == "get_profile"

    input_schema = descriptor["inputSchema"]
    assert input_schema["type"] == "object"
    assert input_schema.get("properties") == {}
    assert not input_schema.get("required")

    output_schema = descriptor["outputSchema"]
    assert output_schema["type"] == "object"
    assert output_schema["additionalProperties"] is False
    assert set(output_schema["properties"]) == {
        "id",
        "name",
    }
    assert set(output_schema["required"]) == {
        "id",
        "name",
    }

    assert descriptor["_meta"]["openai/profile"] is True
    assert descriptor["_meta"]["securitySchemes"] == [
        {
            "type": "oauth2",
            "scopes": ["alovia.read"],
        }
    ]

    annotations = descriptor["annotations"]
    assert annotations["readOnlyHint"] is True
    assert annotations["destructiveHint"] is False
    assert annotations["idempotentHint"] is True
    assert annotations["openWorldHint"] is False


@pytest.mark.asyncio
async def test_get_profile_uses_authenticated_business_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_business_ids: list[UUID] = []

    async def load_profile(
        business_id: UUID,
    ) -> mcp_v2.MCPProfile:
        requested_business_ids.append(business_id)

        return mcp_v2.MCPProfile(
            id=str(business_id),
            name="Empresa A",
        )

    token = AccessToken(
        token="valid",
        client_id="chatgpt-test",
        scopes=["alovia.read"],
        resource=RESOURCE,
        subject="user-test",
        claims={
            "iss": ISSUER,
            "business_id": str(BUSINESS_ID),
        },
    )

    monkeypatch.setattr(
        mcp_v2,
        "get_access_token",
        lambda: token,
    )

    runtime = _runtime(load_profile)

    async with Client(
        runtime.server,
        raise_exceptions=True,
    ) as client:
        result = await client.call_tool(
            "get_profile",
            {},
        )

    payload = result.model_dump(by_alias=True)

    assert requested_business_ids == [BUSINESS_ID]
    assert payload["isError"] is False
    assert payload["structuredContent"] == {
        "id": str(BUSINESS_ID),
        "name": "Empresa A",
    }


@pytest.mark.parametrize(
    "claims",
    (
        {"iss": ISSUER},
        {
            "iss": ISSUER,
            "business_id": "not-a-uuid",
        },
    ),
)
@pytest.mark.asyncio
async def test_get_profile_fails_closed_for_invalid_business_claim(
    monkeypatch: pytest.MonkeyPatch,
    claims: dict[str, str],
) -> None:
    loader_called = False

    async def load_profile(
        business_id: UUID,
    ) -> mcp_v2.MCPProfile:
        nonlocal loader_called
        loader_called = True

        return mcp_v2.MCPProfile(
            id=str(business_id),
            name="Empresa errada",
        )

    token = AccessToken(
        token="valid",
        client_id="chatgpt-test",
        scopes=["alovia.read"],
        resource=RESOURCE,
        subject="user-test",
        claims=claims,
    )

    monkeypatch.setattr(
        mcp_v2,
        "get_access_token",
        lambda: token,
    )

    runtime = _runtime(load_profile)

    async with Client(
        runtime.server,
        raise_exceptions=False,
    ) as client:
        result = await client.call_tool(
            "get_profile",
            {},
        )

    payload = result.model_dump(by_alias=True)

    assert payload["isError"] is True
    assert loader_called is False


@pytest.mark.asyncio
async def test_v2_protected_resource_metadata_is_rfc9728() -> None:
    runtime = _runtime()

    async with runtime.server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=runtime.app,
            ),
            base_url="https://mcp.example.com",
        ) as client:
            response = await client.get(
                "/.well-known/oauth-protected-resource/mcp-v2"
            )

    assert response.status_code == 200

    payload = response.json()

    assert payload["resource"] == RESOURCE
    assert payload["authorization_servers"] == [ISSUER]
    assert payload["scopes_supported"] == ["alovia.read"]
    assert payload["bearer_methods_supported"] == ["header"]


@pytest.mark.asyncio
async def test_v2_requires_bearer_before_initialize() -> None:
    runtime = _runtime()

    async with runtime.server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=runtime.app,
            ),
            base_url="https://mcp.example.com",
        ) as client:
            response = await client.post(
                "/mcp-v2",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

    assert response.status_code == 401

    authenticate = response.headers["www-authenticate"]

    assert 'error="invalid_token"' in authenticate
    assert f'resource_metadata="{METADATA}"' in authenticate


@pytest.mark.asyncio
async def test_v2_rejects_invalid_token() -> None:
    runtime = _runtime()

    async with runtime.server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=runtime.app,
            ),
            base_url="https://mcp.example.com",
        ) as client:
            response = await client.post(
                "/mcp-v2",
                headers={
                    "Authorization": "Bearer invalid",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_v2_rejects_token_for_another_resource() -> None:
    runtime = _runtime()

    async with runtime.server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=runtime.app,
            ),
            base_url="https://mcp.example.com",
        ) as client:
            response = await client.post(
                "/mcp-v2",
                headers={
                    "Authorization": "Bearer wrong-resource",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_v2_rejects_missing_required_scope() -> None:
    runtime = _runtime()

    async with runtime.server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=runtime.app,
            ),
            base_url="https://mcp.example.com",
        ) as client:
            response = await client.post(
                "/mcp-v2",
                headers={
                    "Authorization": "Bearer wrong-scope",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

    assert response.status_code == 403

    assert (
        'error="insufficient_scope"'
        in response.headers["www-authenticate"]
    )


@pytest.mark.asyncio
async def test_v2_accepts_valid_resource_and_scope() -> None:
    runtime = _runtime()

    async with runtime.server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=runtime.app,
            ),
            base_url="https://mcp.example.com",
        ) as client:
            response = await client.post(
                "/mcp-v2",
                headers={
                    "Authorization": "Bearer valid",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json=INITIALIZE_REQUEST,
            )

    assert response.status_code == 200

    payload = response.json()

    assert payload["result"]["serverInfo"]["name"] == "alovia"
    assert payload["result"]["serverInfo"]["version"] == "2.0.0"


@pytest.mark.parametrize(
    ("issuer", "resource"),
    (
        ("http://auth.example.com", RESOURCE),
        (ISSUER, "http://mcp.example.com/mcp-v2"),
        ("not-a-url", RESOURCE),
        (ISSUER, "not-a-url"),
    ),
)
def test_v2_rejects_insecure_or_invalid_urls(
    issuer: str,
    resource: str,
) -> None:
    with pytest.raises(MCPConfigurationError):
        build_mcp_v2_runtime(
            allowed_hosts=("mcp.example.com",),
            issuer_url=issuer,
            resource_server_url=resource,
            token_verifier=StaticTokenVerifier(),
        )
