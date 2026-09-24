from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.conversations.constants import ConversationState
from app.conversations.outbound import OutboundMessage


@dataclass(frozen=True, slots=True)
class ConversationInput:
    business_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    provider_message_id: str
    message_type: str
    body: str | None
    interactive_id: str | None
    whatsapp_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    business_id: uuid.UUID
    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    state: str
    context: dict[str, Any]
    automation_enabled: bool
    handoff_status: str
    assistant_enabled: bool = True
    greeting_message: str = "Olá! Como posso ajudar com seu ar-condicionado?"
    fallback_message: str = (
        "Não entendi. Conte em poucas palavras o serviço que você precisa."
    )
    handoff_message: str = (
        "Seu atendimento foi encaminhado para uma pessoa da equipe."
    )
    customer_name: str | None = None
    whatsapp_profile_name: str | None = None
    business_timezone: str = "America/Sao_Paulo"


@dataclass(frozen=True, slots=True)
class ConversationTransition:
    state: ConversationState
    context: dict[str, Any]
    automation_enabled: bool
    handoff_status: str
    outbound: OutboundMessage
    customer_name: str | None = None
