from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.conversations.constants import (
    BOOKING_BACK,
    BOOKING_CANCEL,
    BOOKING_CONFIRM,
    CANCEL_ABORT,
    CANCEL_CONFIRM,
    RESCHEDULE_CONFIRM,
    ACCESS_DIFFICULT,
    ACCESS_NORMAL,
    ACCESS_UNKNOWN,
    MENU_BOOK,
    MENU_CANCEL,
    MENU_HUMAN,
    MENU_RESCHEDULE,
    QUANTITY_OPTION_LIMIT,
    SITE_LIMIT_17,
    SITE_LIMIT_18,
    SITE_LIMIT_NONE,
    TUBING_CONFIRM,
    TUBING_UNKNOWN,
)
from app.conversations.ports import BookingOption

LIST_BUTTON_TEXT = "Ver opções"
MAX_LIST_ROWS = 10


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


def name_request_message(body: str) -> OutboundMessage:
    return OutboundMessage(message_type="text", body=body)


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


def weekday_selection_message(
    options: Sequence[BookingOption],
    *,
    body: str,
) -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body=body,
        interactive_id="booking.weekdays",
        outbound_payload=_list_payload("Dias da semana", options, prefix="weekday:"),
    )


def date_selection_message(
    options: Sequence[BookingOption],
    *,
    body: str = "Escolha uma data para o atendimento.",
) -> OutboundMessage:
    safe_body = _append_hidden_options_hint(body, options, "data")
    return OutboundMessage(
        message_type="interactive_list",
        body=safe_body,
        interactive_id="booking.dates",
        outbound_payload=_list_payload("Datas", options, prefix="date:"),
    )


def quantity_selection_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_list",
        body="Quantos itens ou aparelhos são?",
        interactive_id="booking.quantity",
        outbound_payload=_list_payload(
            "Quantidade",
            tuple(
                BookingOption(f"quantity:{value}", str(value))
                for value in range(1, QUANTITY_OPTION_LIMIT + 1)
            ),
        ),
    )


def access_selection_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="O local de acesso é alto ou difícil?",
        interactive_id="booking.access",
        outbound_payload=_button_payload(
            (
                BookingOption(ACCESS_NORMAL, "Não"),
                BookingOption(ACCESS_DIFFICULT, "Sim"),
                BookingOption(ACCESS_UNKNOWN, "Não sei"),
            )
        ),
    )


def address_request_message(
    body: str = "Qual é o endereço completo do serviço, incluindo a cidade?",
) -> OutboundMessage:
    return OutboundMessage(message_type="text", body=body)


def tubing_length_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body=(
            "Você sabe aproximadamente quantos metros de tubulação serão "
            "necessários entre as unidades?"
        ),
    )


def tubing_guidance_message(
    included_meters: Decimal | None,
    extra_meter_price: Decimal | None,
) -> OutboundMessage:
    included = included_meters if included_meters is not None else Decimal("3")
    if included == included.to_integral():
        included_label = str(int(included))
    else:
        included_label = str(included).replace(".", ",")

    body = (
        f"A instalação padrão considera até {included_label} metros. "
        "Se você não souber a medida exata, sem problema: o técnico pode "
        "conferir no local."
    )
    if extra_meter_price is not None and extra_meter_price > 0:
        body += (
            " Se precisar de tubulação adicional, o valor cadastrado é "
            f"{_format_brl(extra_meter_price)} por metro."
        )
    else:
        body += " Se precisar de material adicional, o valor pode variar."
    return OutboundMessage(message_type="text", body=body)


def tubing_confirmation_message(meters: Decimal) -> OutboundMessage:
    value = str(meters.normalize()).replace(".", ",")
    return OutboundMessage(
        message_type="interactive_button",
        body=f"Entendi. Você estima cerca de {value} metros, certo?",
        interactive_id="booking.tubing_confirmation",
        outbound_payload=_button_payload(
            (
                BookingOption(TUBING_CONFIRM, "Sim"),
                BookingOption(TUBING_UNKNOWN, "Não tenho certeza"),
            )
        ),
    )


def site_limit_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="Existe algum horário limite para realizar o serviço no local?",
        interactive_id="booking.site_limit",
        outbound_payload=_button_payload(
            (
                BookingOption(SITE_LIMIT_NONE, "Não"),
                BookingOption(SITE_LIMIT_17, "Até 17h"),
                BookingOption(SITE_LIMIT_18, "Até 18h"),
            )
        ),
    )


def time_selection_message(
    options: Sequence[BookingOption],
    *,
    body: str = "Escolha um horário disponível.",
) -> OutboundMessage:
    safe_body = _append_hidden_options_hint(body, options, "horário")
    return OutboundMessage(
        message_type="interactive_list",
        body=safe_body,
        interactive_id="booking.times",
        outbound_payload=_list_payload("Horários", options, prefix="time:"),
    )


def booking_confirmation_message(
    body: str = "Confirme os dados do agendamento.",
) -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body=body,
        interactive_id="booking.confirmation",
        outbound_payload=_button_payload(
            (
                BookingOption(BOOKING_CONFIRM, "Confirmar"),
                BookingOption(BOOKING_BACK, "Voltar"),
                BookingOption(BOOKING_CANCEL, "Cancelar"),
            )
        ),
    )


def booking_completed_message(
    body: str = "Agendamento confirmado com sucesso.",
) -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body=body,
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
        body="Esse horário acabou de ficar indisponível. Escolha outro horário.",
        interactive_id="booking.slot_unavailable",
        outbound_payload=_button_payload(
            (
                BookingOption(BOOKING_BACK, "Voltar"),
                BookingOption(BOOKING_CANCEL, "Cancelar"),
            )
        ),
    )


def existing_booking_selection_message(
    options: Sequence[BookingOption],
    *,
    purpose: str,
) -> OutboundMessage:
    body = (
        "Qual agendamento você quer reagendar?"
        if purpose == "reschedule"
        else "Qual agendamento você quer cancelar?"
    )
    return OutboundMessage(
        message_type="interactive_list",
        body=body,
        interactive_id=f"{purpose}.appointments",
        outbound_payload=_list_payload(
            "Agendamentos",
            options,
            prefix="appointment:",
        ),
    )


def reschedule_confirmation_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="Confirmar o novo dia e horário deste agendamento?",
        interactive_id="reschedule.confirmation",
        outbound_payload=_button_payload(
            (
                BookingOption(RESCHEDULE_CONFIRM, "Confirmar"),
                BookingOption(BOOKING_BACK, "Voltar"),
                BookingOption(BOOKING_CANCEL, "Sair"),
            )
        ),
    )


def reschedule_completed_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Agendamento reagendado com sucesso.",
    )


def cancel_confirmation_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="Tem certeza que deseja cancelar este agendamento?",
        interactive_id="cancel.confirmation",
        outbound_payload=_button_payload(
            (
                BookingOption(CANCEL_CONFIRM, "Cancelar agendamento"),
                BookingOption(CANCEL_ABORT, "Manter agendamento"),
            )
        ),
    )


def cancel_completed_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Agendamento cancelado com sucesso.",
    )


def reschedule_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Não encontrei um agendamento futuro para reagendar.",
    )


def cancel_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Não encontrei um agendamento futuro para cancelar.",
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
    visible = tuple(options[:MAX_LIST_ROWS])
    return {
        "button": LIST_BUTTON_TEXT,
        "sections": [
            {
                "title": section_title,
                "rows": [
                    {"id": f"{prefix}{option.id}", "title": option.label}
                    for option in visible
                ],
            }
        ],
    }


def _append_hidden_options_hint(
    body: str,
    options: Sequence[BookingOption],
    noun: str,
) -> str:
    if len(options) <= MAX_LIST_ROWS:
        return body
    labels = ", ".join(option.label for option in options)
    expanded = (
        f"{body.rstrip()}\n\nOpções disponíveis: {labels}. "
        f"Se o {noun} que você prefere não aparecer em “Ver opções”, "
        f"responda digitando o {noun}."
    )
    return expanded[:1024]


def _button_payload(options: Sequence[BookingOption]) -> dict[str, Any]:
    return {
        "buttons": [
            {"id": option.id, "title": option.label}
            for option in options
        ]
    }


def _format_brl(value: Decimal) -> str:
    normalized = f"{value:,.2f}"
    return f"R$ {normalized.replace(',', '#').replace('.', ',').replace('#', '.')}"
