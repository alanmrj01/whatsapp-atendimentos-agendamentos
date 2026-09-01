from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.conversations.constants import (
    BOOKING_BACK,
    BOOKING_CANCEL,
    BOOKING_CONFIRM,
    MENU_BOOK,
    MENU_CANCEL,
    MENU_HUMAN,
    MENU_RESCHEDULE,
)
from app.conversations.ports import BookingOption

LIST_BUTTON_TEXT = "Ver opções"


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    message_type: str
    body: str | None
    interactive_id: str | None = None
    outbound_payload: dict[str, Any] | None = None


def main_menu_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body="Como podemos ajudar? Escolha uma opção no menu.",
        interactive_id="menu.main",
        outbound_payload=_list_payload(
            "Atendimento",
            (
                BookingOption(MENU_BOOK, "Agendar"),
                BookingOption(MENU_RESCHEDULE, "Reagendar"),
                BookingOption(MENU_CANCEL, "Cancelar"),
                BookingOption(MENU_HUMAN, "Falar com atendente"),
            ),
        ),
    )


def service_selection_message(
    options: Sequence[BookingOption],
    *,
    body: str = "Escolha um serviço para continuar.",
) -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body=body,
        interactive_id="booking.services",
        outbound_payload=_list_payload("Serviços", options, prefix="service:"),
    )


def date_selection_message(
    options: Sequence[BookingOption],
    *,
    body: str = "Escolha uma data para o atendimento.",
) -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body=body,
        interactive_id="booking.dates",
        outbound_payload=_list_payload("Datas", options, prefix="date:"),
    )


def time_selection_message(
    options: Sequence[BookingOption],
    *,
    body: str = "Escolha um horário disponível.",
) -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body=body,
        interactive_id="booking.times",
        outbound_payload=_list_payload("Horários", options, prefix="time:"),
    )


def booking_confirmation_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="Confirme os dados do agendamento.",
        interactive_id="booking.confirmation",
        outbound_payload=_button_payload(
            (
                BookingOption(BOOKING_CONFIRM, "Confirmar"),
                BookingOption(BOOKING_BACK, "Voltar"),
                BookingOption(BOOKING_CANCEL, "Cancelar"),
            )
        ),
    )


def booking_completed_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Solicitação de agendamento concluída.",
    )


def booking_cancelled_message() -> OutboundMessage:
    message = main_menu_message()
    return OutboundMessage(
        message_type=message.message_type,
        body="Fluxo de agendamento encerrado. Escolha uma opção no menu.",
        interactive_id=message.interactive_id,
        outbound_payload=message.outbound_payload,
    )


def booking_unavailable_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="O agendamento está temporariamente indisponível.",
    )


def no_services_message() -> OutboundMessage:
    message = main_menu_message()
    return OutboundMessage(
        message_type=message.message_type,
        body="Não há serviços disponíveis no momento. Escolha outra opção.",
        interactive_id=message.interactive_id,
        outbound_payload=message.outbound_payload,
    )


def slot_unavailable_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="Esse horário não está mais disponível.",
        interactive_id="booking.slot_unavailable",
        outbound_payload=_button_payload(
            (
                BookingOption(BOOKING_BACK, "Voltar"),
                BookingOption(BOOKING_CANCEL, "Cancelar"),
            )
        ),
    )


def reschedule_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="O fluxo de reagendamento será disponibilizado em breve.",
    )


def cancel_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="O fluxo de cancelamento será disponibilizado em breve.",
    )


def handoff_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Seu atendimento foi encaminhado para uma pessoa da equipe.",
    )


def _list_payload(
    section_title: str,
    options: Sequence[BookingOption],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    return {
        "button": LIST_BUTTON_TEXT,
        "sections": [
            {
                "title": section_title,
                "rows": [
                    {"id": f"{prefix}{option.id}", "title": option.label}
                    for option in options
                ],
            }
        ],
    }


def _button_payload(options: Sequence[BookingOption]) -> dict[str, Any]:
    return {
        "buttons": [
            {"id": option.id, "title": option.label}
            for option in options
        ]
    }
