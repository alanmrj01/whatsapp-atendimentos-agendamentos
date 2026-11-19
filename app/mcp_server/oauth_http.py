from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rate_limit import login_rate_limiter
from app.auth.security import normalize_email
from app.core.database import get_db
from app.mcp_server.oauth_authorization import (
    AuthorizationFlowError,
    OAuthAuthorizationCoordinator,
    SQLAlchemyAuthorizationFlowStore,
)
from app.mcp_server.oauth_lifecycle import (
    OAuthGrantError,
    OAuthLifecycle,
    OAuthTokenSet,
    SQLAlchemyOAuthStore,
)
from app.mcp_server.oauth_protocol import (
    CHATGPT_CLIENT_ID,
    OAuthProtocolError,
    authorization_redirect,
    authorization_server_metadata,
    validate_authorization_request,
    validate_chatgpt_cimd,
    validate_token_request,
)


MAX_CIMD_BYTES = 64 * 1024


class ClientMetadataVerifier(Protocol):
    async def verify(self) -> None: ...


class ChatGPTClientMetadataVerifier:
    async def verify(self) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=5.0,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    CHATGPT_CLIENT_ID,
                    headers={
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError:
            raise OAuthProtocolError(
                "client metadata unavailable"
            ) from None

        if (
            response.status_code != 200
            or len(response.content) > MAX_CIMD_BYTES
            or str(response.url) != CHATGPT_CLIENT_ID
        ):
            raise OAuthProtocolError(
                "client metadata unavailable"
            )

        try:
            document = response.json()
        except ValueError:
            raise OAuthProtocolError(
                "invalid client metadata"
            ) from None

        if not isinstance(document, Mapping):
            raise OAuthProtocolError(
                "invalid client metadata"
            )

        validate_chatgpt_cimd(document)


@dataclass(frozen=True, slots=True)
class OAuthHTTPServices:
    coordinator: OAuthAuthorizationCoordinator
    lifecycle: OAuthLifecycle


ServicesDependency = Callable[
    [],
    Awaitable[OAuthHTTPServices],
]


def _oauth_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; "
            "style-src 'unsafe-inline'; "
            "form-action 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        ),
    }


def _json(
    content: dict[str, object],
    *,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers=_oauth_headers(),
    )


def _html(
    body: str,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return HTMLResponse(
        content=body,
        status_code=status_code,
        headers=_oauth_headers(),
    )


def _page(
    *,
    title: str,
    content: str,
) -> str:
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body {{
  font-family: system-ui, sans-serif;
  max-width: 440px;
  margin: 48px auto;
  padding: 0 20px;
  color: #171717;
}}
h1 {{ font-size: 24px; }}
p {{ line-height: 1.5; }}
label {{ display:block; margin:14px 0 6px; }}
input, button {{
  box-sizing:border-box;
  width:100%;
  padding:12px;
  font:inherit;
}}
button {{ margin-top:16px; cursor:pointer; }}
.choice {{
  display:flex;
  gap:8px;
  align-items:flex-start;
  margin:12px 0;
}}
.choice input {{
  width:auto;
  margin-top:4px;
}}
.secondary {{ margin-top:8px; }}
.error {{ color:#9b1c1c; }}
</style>
</head>
<body>
{content}
</body>
</html>"""


def _login_page(
    flow_token: str,
    *,
    error: str | None = None,
) -> HTMLResponse:
    error_html = (
        '<p class="error">'
        + html.escape(error)
        + "</p>"
        if error
        else ""
    )

    token = html.escape(
        flow_token,
        quote=True,
    )

    return _html(
        _page(
            title="Conectar Alovia",
            content=f"""
<h1>Conectar sua conta Alovia</h1>
<p>
Entre com sua conta Alovia para autorizar
o acesso solicitado pelo ChatGPT.
</p>
{error_html}
<form method="post" action="/oauth/login">
<input type="hidden" name="flow_token" value="{token}">
<label for="email">E-mail</label>
<input id="email" name="email" type="email"
       autocomplete="username" required maxlength="254">
<label for="password">Senha</label>
<input id="password" name="password" type="password"
       autocomplete="current-password" required maxlength="1024">
<button type="submit">Continuar</button>
</form>
""",
        )
    )


def _consent_page(
    *,
    flow_token: str,
    memberships,
) -> HTMLResponse:
    token = html.escape(
        flow_token,
        quote=True,
    )

    choices: list[str] = []

    for index, membership in enumerate(memberships):
        business_id = html.escape(
            str(membership.business_id),
            quote=True,
        )
        business_name = html.escape(
            membership.business_name
        )
        checked = " checked" if index == 0 else ""

        choices.append(
            f"""
<label class="choice">
<input type="radio"
       name="business_id"
       value="{business_id}"{checked} required>
<span>{business_name}</span>
</label>
"""
        )

    return _html(
        _page(
            title="Autorizar Alovia",
            content=f"""
<h1>Autorizar acesso</h1>
<p>
O ChatGPT solicita acesso de leitura aos dados
da empresa Alovia que você escolher.
</p>
<form method="post" action="/oauth/consent">
<input type="hidden" name="flow_token" value="{token}">
{''.join(choices)}
<button type="submit" name="action" value="approve">
Autorizar
</button>
<button class="secondary"
        type="submit"
        name="action"
        value="deny">
Cancelar
</button>
</form>
""",
        )
    )


def _token_payload(
    token_set: OAuthTokenSet,
) -> dict[str, object]:
    return {
        "access_token": token_set.access_token,
        "token_type": token_set.token_type,
        "expires_in": token_set.expires_in,
        "refresh_token": token_set.refresh_token,
        "scope": token_set.scope,
    }


def _protocol_error(
    error: OAuthProtocolError,
) -> JSONResponse:
    message = str(error)

    if message == "invalid client":
        return _json(
            {"error": "invalid_client"},
            status_code=401,
        )

    if message == "unsupported grant type":
        return _json(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    if message == "invalid resource":
        return _json(
            {"error": "invalid_target"},
            status_code=400,
        )

    return _json(
        {"error": "invalid_request"},
        status_code=400,
    )


async def _form_data(
    request: Request,
) -> dict[str, str]:
    form = await request.form()

    result: dict[str, str] = {}

    for key, value in form.multi_items():
        if not isinstance(value, str):
            raise OAuthProtocolError(
                "invalid form"
            )

        if key in result:
            raise OAuthProtocolError(
                "duplicate form parameter"
            )

        result[key] = value

    return result


def build_oauth_router(
    *,
    issuer: str,
    resource: str,
    signing_key: str,
    metadata_verifier: ClientMetadataVerifier | None = None,
    services_dependency: Callable[..., Awaitable[OAuthHTTPServices]]
    | None = None,
) -> APIRouter:
    metadata = authorization_server_metadata(
        issuer=issuer
    )

    verifier = (
        metadata_verifier
        or ChatGPTClientMetadataVerifier()
    )

    if services_dependency is None:

        async def production_services(
            db: AsyncSession = Depends(get_db),
        ) -> OAuthHTTPServices:
            return OAuthHTTPServices(
                coordinator=OAuthAuthorizationCoordinator(
                    store=SQLAlchemyAuthorizationFlowStore(
                        db
                    )
                ),
                lifecycle=OAuthLifecycle(
                    store=SQLAlchemyOAuthStore(db),
                    issuer=issuer,
                    resource=resource,
                    signing_key=signing_key,
                ),
            )

        dependency = production_services
    else:
        dependency = services_dependency

    router = APIRouter(tags=["mcp-oauth"])

    @router.get(
        "/.well-known/oauth-authorization-server"
    )
    async def oauth_metadata():
        return _json(metadata)

    @router.get("/oauth/authorize")
    async def authorize(
        request: Request,
        services: OAuthHTTPServices = Depends(
            dependency
        ),
    ):
        params = dict(
            request.query_params.multi_items()
        )

        if len(params) != len(
            request.query_params.multi_items()
        ):
            return _json(
                {"error": "invalid_request"},
                status_code=400,
            )

        try:
            validated = validate_authorization_request(
                params,
                expected_resource=resource,
            )
            await verifier.verify()
            pending = await services.coordinator.begin(
                validated
            )
        except OAuthProtocolError as exc:
            return _protocol_error(exc)
        except AuthorizationFlowError:
            return _json(
                {"error": "server_error"},
                status_code=500,
            )

        return _login_page(
            pending.flow_token
        )

    @router.post("/oauth/login")
    async def oauth_login(
        request: Request,
        services: OAuthHTTPServices = Depends(
            dependency
        ),
    ):
        try:
            form = await _form_data(request)
        except OAuthProtocolError:
            return _json(
                {"error": "invalid_request"},
                status_code=400,
            )

        flow_token = form.get(
            "flow_token",
            "",
        )
        email = form.get("email", "")
        password = form.get("password", "")

        try:
            try:
                rate_key = normalize_email(email)
            except ValueError:
                rate_key = email.strip().lower()[:254]

            await login_rate_limiter.acquire(
                rate_key
            )

            authenticated = (
                await services.coordinator.authenticate(
                    flow_token=flow_token,
                    email=email,
                    password=password,
                )
            )

            await login_rate_limiter.success(
                rate_key
            )
        except AuthorizationFlowError as exc:
            if exc.error == "access_denied":
                return _login_page(
                    flow_token,
                    error="E-mail ou senha inválidos.",
                )

            return _json(
                {"error": exc.error},
                status_code=400,
            )

        return _consent_page(
            flow_token=authenticated.flow_token,
            memberships=authenticated.memberships,
        )

    @router.post("/oauth/consent")
    async def oauth_consent(
        request: Request,
        services: OAuthHTTPServices = Depends(
            dependency
        ),
    ):
        try:
            form = await _form_data(request)
        except OAuthProtocolError:
            return _json(
                {"error": "invalid_request"},
                status_code=400,
            )

        flow_token = form.get(
            "flow_token",
            "",
        )
        action = form.get(
            "action",
            "",
        )

        if action == "deny":
            try:
                denial = (
                    await services.coordinator.deny(
                        flow_token=flow_token
                    )
                )
                location = authorization_redirect(
                    issuer=issuer,
                    redirect_uri=denial.redirect_uri,
                    state=denial.state,
                    error="access_denied",
                )
            except (
                AuthorizationFlowError,
                OAuthProtocolError,
            ):
                return _json(
                    {"error": "invalid_request"},
                    status_code=400,
                )

            return RedirectResponse(
                location,
                status_code=303,
                headers=_oauth_headers(),
            )

        if action != "approve":
            return _json(
                {"error": "invalid_request"},
                status_code=400,
            )

        try:
            business_id = UUID(
                form.get("business_id", "")
            )
        except ValueError:
            return _json(
                {"error": "invalid_request"},
                status_code=400,
            )

        try:
            completed = (
                await services.coordinator.authorize_business(
                    flow_token=flow_token,
                    business_id=business_id,
                    issuer=services.lifecycle,
                )
            )

            location = authorization_redirect(
                issuer=issuer,
                redirect_uri=completed.redirect_uri,
                state=completed.state,
                code=completed.code,
            )
        except AuthorizationFlowError as exc:
            return _json(
                {"error": exc.error},
                status_code=400,
            )
        except OAuthProtocolError:
            return _json(
                {"error": "invalid_request"},
                status_code=400,
            )

        return RedirectResponse(
            location,
            status_code=303,
            headers=_oauth_headers(),
        )

    @router.post("/oauth/token")
    async def oauth_token(
        request: Request,
        services: OAuthHTTPServices = Depends(
            dependency
        ),
    ):
        try:
            form = await _form_data(request)

            parsed = validate_token_request(
                form,
                expected_resource=resource,
            )

            if parsed.grant_type == "authorization_code":
                issued = await (
                    services.lifecycle
                    .exchange_authorization_code(
                        parsed
                    )
                )
            else:
                issued = await services.lifecycle.refresh(
                    parsed
                )
        except OAuthProtocolError as exc:
            return _protocol_error(exc)
        except OAuthGrantError as exc:
            return _json(
                {
                    "error": exc.error,
                    "error_description": exc.description,
                },
                status_code=400,
            )

        return _json(
            _token_payload(issued)
        )

    return router
