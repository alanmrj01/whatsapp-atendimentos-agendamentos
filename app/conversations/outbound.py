from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    message_type: str
    body: str | None
    interactive_id: str | None = None


def main_menu_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body="Como podemos ajudar? Escolha uma opção no menu.",
        interactive_id="menu.main",
    )


def service_selection_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body="Escolha um serviço para continuar.",
        interactive_id="booking.services",
    )


def date_selection_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body="Escolha uma data para o atendimento.",
        interactive_id="booking.dates",
    )


def time_selection_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body="Escolha um horário disponível.",
        interactive_id="booking.times",
    )


def booking_confirmation_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="Confirme os dados do agendamento.",
        interactive_id="booking.confirmation",
    )


def booking_completed_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Solicitação de agendamento concluída.",
    )


def booking_cancelled_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body="Fluxo de agendamento encerrado. Voltamos ao menu principal.",
        interactive_id="menu.main",
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
