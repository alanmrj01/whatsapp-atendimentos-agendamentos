from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import date
from typing import Any
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from app.core.database import get_session_factory
from app.models.domain import Business
from app.operations.schemas import (
    AppointmentList,
    AutomationSettingsView,
    BusinessHoursView,
    BusinessView,
    ConversationDetail,
    ConversationList,
    CustomerList,
    EmployeeList,
    ServiceList,
    WorkingHoursList,
)
from app.operations.service import OperationalService
from app.mcp_server.runtime import (
    MCPConfigurationError,
    MCPRuntime,
    normalize_allowed_hosts,
)


_http_url = TypeAdapter(AnyHttpUrl)


class MCPProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


BusinessProfileLoader = Callable[
    [UUID],
    Awaitable[MCPProfile | None],
]

OperationsReadLoader = Callable[
    [UUID, str, dict[str, Any]],
    Awaitable[Any],
]


def _authenticated_business_id() -> UUID:
    access_token = get_access_token()

    claims = (
        access_token.claims
        if access_token is not None
        else None
    )

    raw_business_id = (
        claims.get("business_id")
        if isinstance(claims, dict)
        else None
    )

    if not isinstance(raw_business_id, str):
        raise ValueError(
            "Authenticated business is unavailable"
        )

    try:
        return UUID(raw_business_id)
    except ValueError:
        raise ValueError(
            "Authenticated business is unavailable"
        ) from None


async def _load_operations_read(
    business_id: UUID,
    operation: str,
    kwargs: dict[str, Any],
) -> Any:
    session_factory = get_session_factory()

    async with session_factory() as session:
        service = OperationalService(session)

        handlers = {
            "get_business": service.get_business,
            "get_business_hours": service.get_business_hours,
            "list_working_hours": service.list_working_hours,
            "get_automation": service.get_automation,
            "list_employees": service.list_employees,
            "list_services": service.list_services,
            "list_customers": service.list_customers,
            "list_appointments": service.list_appointments,
            "list_conversations": service.list_conversations,
            "get_conversation": service.get_conversation,
        }

        handler = handlers.get(operation)

        if handler is None:
            raise ValueError(
                "Unsupported MCP read operation"
            )

        return await handler(
            business_id,
            **kwargs,
        )


async def _load_business_profile(
    business_id: UUID,
) -> MCPProfile | None:
    session_factory = get_session_factory()

    async with session_factory() as session:
        business = await session.get(
            Business,
            business_id,
        )

    if business is None or not business.active:
        return None

    name = business.name.strip()

    if not name:
        return None

    return MCPProfile(
        id=str(business.id),
        name=name,
    )


def _secure_http_url(value: str, *, field: str) -> AnyHttpUrl:
    try:
        parsed = _http_url.validate_python(value.strip())
    except ValidationError:
        raise MCPConfigurationError(
            f"Invalid MCP V2 {field}"
        ) from None

    if parsed.scheme != "https":
        raise MCPConfigurationError(
            f"MCP V2 {field} must use HTTPS"
        )

    if not parsed.host:
        raise MCPConfigurationError(
            f"Invalid MCP V2 {field}"
        )

    return parsed


def build_mcp_v2_runtime(
    *,
    allowed_hosts: Iterable[str],
    issuer_url: str,
    resource_server_url: str,
    token_verifier: TokenVerifier,
    profile_loader: BusinessProfileLoader | None = None,
    operations_read_loader: OperationsReadLoader | None = None,
    required_scope: str = "alovia.read",
) -> MCPRuntime:
    hosts = normalize_allowed_hosts(allowed_hosts)

    issuer = issuer_url.strip()
    _secure_http_url(
        issuer,
        field="issuer URL",
    )
    resource = _secure_http_url(
        resource_server_url,
        field="resource URL",
    )

    scope = required_scope.strip()
    if not scope or any(character.isspace() for character in scope):
        raise MCPConfigurationError(
            "Invalid MCP V2 required scope"
        )

    load_profile = (
        profile_loader
        if profile_loader is not None
        else _load_business_profile
    )

    load_operations = (
        operations_read_loader
        if operations_read_loader is not None
        else _load_operations_read
    )

    server = MCPServer(
        "alovia",
        title="Alovia MCP V2",
        description="Servidor MCP autenticado da Alovia.",
        instructions=(
            "Servidor autenticado da Alovia. "
            "Ferramentas privadas usam exclusivamente a empresa "
            "associada às credenciais OAuth da conexão."
        ),
        version="2.0.0",
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=resource,
            required_scopes=[scope],
            validate_token_resource=True,
        ),
    )

    @server.tool(
        name="get_profile",
        title="Perfil da empresa Alovia",
        description=(
            "Retorna o perfil da empresa Alovia representada "
            "pelas credenciais autenticadas desta conexão."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        meta={
            "openai/profile": True,
            "securitySchemes": [
                {
                    "type": "oauth2",
                    "scopes": [scope],
                }
            ],
        },
        structured_output=True,
    )
    async def get_profile() -> MCPProfile:
        business_id = _authenticated_business_id()

        profile = await load_profile(business_id)

        if profile is None:
            raise ValueError(
                "Authenticated business is unavailable"
            )

        return profile


    async def _read_operation(
        operation: str,
        **kwargs: Any,
    ) -> Any:
        business_id = _authenticated_business_id()

        return await load_operations(
            business_id,
            operation,
            kwargs,
        )

    def _read_annotations() -> ToolAnnotations:
        return ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )

    def _read_meta() -> dict[str, object]:
        return {
            "securitySchemes": [
                {
                    "type": "oauth2",
                    "scopes": [scope],
                }
            ]
        }

    @server.tool(
        name="get_business",
        title="Configuração da empresa",
        description=(
            "Retorna a configuração operacional da empresa "
            "associada às credenciais autenticadas."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def get_business() -> BusinessView:
        return await _read_operation(
            "get_business"
        )

    @server.tool(
        name="get_business_hours",
        title="Horários da empresa",
        description=(
            "Retorna os dias e horários gerais de "
            "funcionamento da empresa autenticada."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def get_business_hours() -> BusinessHoursView:
        return await _read_operation(
            "get_business_hours"
        )

    @server.tool(
        name="list_working_hours",
        title="Jornadas da equipe",
        description=(
            "Lista as jornadas configuradas dos funcionários "
            "da empresa autenticada."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def list_working_hours() -> WorkingHoursList:
        items = await _read_operation(
            "list_working_hours"
        )

        return WorkingHoursList(
            items=items
        )

    @server.tool(
        name="get_automation_settings",
        title="Configuração do atendimento automático",
        description=(
            "Retorna as configurações efetivas do atendimento "
            "automático da empresa, sem alterá-las."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def get_automation_settings() -> AutomationSettingsView:
        return await _read_operation(
            "get_automation"
        )

    @server.tool(
        name="list_employees",
        title="Equipe",
        description=(
            "Lista os funcionários e seus vínculos de serviços "
            "na empresa autenticada."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def list_employees() -> EmployeeList:
        items = await _read_operation(
            "list_employees"
        )

        return EmployeeList(
            items=items
        )

    @server.tool(
        name="list_services",
        title="Serviços",
        description=(
            "Lista os serviços configurados da empresa, "
            "incluindo duração, preço e status."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def list_services() -> ServiceList:
        items = await _read_operation(
            "list_services"
        )

        return ServiceList(
            items=items
        )

    @server.tool(
        name="list_customers",
        title="Clientes",
        description=(
            "Lista clientes da empresa autenticada. "
            "A resposta é limitada a no máximo 100 registros."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def list_customers(
        limit: int = 50,
    ) -> CustomerList:
        if limit < 1 or limit > 100:
            raise ValueError(
                "limit must be between 1 and 100"
            )

        items = await _read_operation(
            "list_customers"
        )

        return CustomerList(
            items=items[:limit]
        )

    @server.tool(
        name="list_appointments",
        title="Agenda",
        description=(
            "Lista os agendamentos da empresa autenticada. "
            "Sem data informada, usa o dia atual da empresa."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def list_appointments(
        selected_date: date | None = None,
    ) -> AppointmentList:
        items = await _read_operation(
            "list_appointments",
            selected_date=selected_date,
            starts_at=None,
            ends_before=None,
        )

        return AppointmentList(
            items=items
        )

    @server.tool(
        name="list_conversations",
        title="Conversas",
        description=(
            "Lista conversas da empresa autenticada com "
            "paginação e filtros opcionais."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def list_conversations(
        search: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> ConversationList:
        if page < 1:
            raise ValueError(
                "page must be at least 1"
            )

        if page_size < 1 or page_size > 50:
            raise ValueError(
                "page_size must be between 1 and 50"
            )

        normalized_search = (
            search.strip()
            if isinstance(search, str)
            and search.strip()
            else None
        )

        normalized_status = (
            status.strip()
            if isinstance(status, str)
            and status.strip()
            else None
        )

        if (
            normalized_search is not None
            and len(normalized_search) > 120
        ):
            raise ValueError(
                "search is too long"
            )

        if (
            normalized_status is not None
            and len(normalized_status) > 40
        ):
            raise ValueError(
                "status is too long"
            )

        items, total = await _read_operation(
            "list_conversations",
            search=normalized_search,
            status=normalized_status,
            page=page,
            page_size=page_size,
        )

        return ConversationList(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @server.tool(
        name="get_conversation",
        title="Detalhes da conversa",
        description=(
            "Retorna os detalhes e o histórico de uma conversa "
            "somente se ela pertencer à empresa autenticada."
        ),
        annotations=_read_annotations(),
        meta=_read_meta(),
        structured_output=True,
    )
    async def get_conversation(
        conversation_id: UUID,
    ) -> ConversationDetail:
        return await _read_operation(
            "get_conversation",
            conversation_id=conversation_id,
        )

    app = server.streamable_http_app(
        streamable_http_path="/mcp-v2",
        json_response=True,
        stateless_http=True,
        max_request_body_size=1_048_576,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(hosts),
            allowed_origins=[],
        ),
    )

    return MCPRuntime(
        server=server,
        app=app,
    )
