from __future__ import annotations

import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.conversations.constants import (
    ADDRESS_CITY_CONFIRM,
    ADDRESS_CITY_OTHER,
    EQUIPMENT_BOTH,
    EQUIPMENT_HAS,
    EQUIPMENT_INSTALLATION,
    EQUIPMENT_MODEL_KNOWN,
    EQUIPMENT_MODEL_RECOMMEND,
    EQUIPMENT_NEEDS,
    EQUIPMENT_PREF_COST_BENEFIT,
    EQUIPMENT_PREF_ECONOMY,
    EQUIPMENT_PREF_MODERN,
    EQUIPMENT_CYCLE_COOLING,
    EQUIPMENT_CYCLE_HEAT_COOL,
    EQUIPMENT_INDOOR_SPACE_NO,
    EQUIPMENT_INDOOR_SPACE_YES,
    EQUIPMENT_OUTDOOR_SPACE_NO,
    EQUIPMENT_OUTDOOR_SPACE_YES,
    EQUIPMENT_PURCHASE,
    HEIGHT_AT_MOST_3M,
    HEIGHT_OVER_3M,
    MEDIA_CONTINUE_TEXT,
    MEDIA_HUMAN_CONFIRM,
    PROPERTY_BUILDING,
    PROPERTY_CONDOMINIUM,
    PROPERTY_HOUSE,
    ATTENDEE_CUSTOMER,
    ATTENDEE_OTHER,
    PHONE_CONFIRM,
    PHONE_OTHER,
    QUOTE_FINISH,
    QUOTE_SCHEDULE,
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


def split_outbound_message(
    message: OutboundMessage,
    *,
    max_lines: int = 4,
    approximate_line_width: int = 72,
) -> tuple[OutboundMessage, ...]:
    """Split at semantic boundaries and keep interaction on the final chunk.

    WhatsApp wrapping varies by device. The character width is therefore only a
    conservative estimate; explicit newlines and sentence boundaries remain the
    primary split points.
    """

    if max_lines < 1:
        raise ValueError("max_lines must be positive")
    if message.body is None:
        return (message,)
    if approximate_line_width < 20:
        raise ValueError("approximate_line_width is too small")
    groups = _semantic_line_groups(message.body, approximate_line_width)
    if sum(len(group) for group in groups) <= max_lines:
        return (message,)
    chunks: list[list[str]] = []
    current: list[str] = []
    for group in groups:
        if current and len(current) + len(group) > max_lines:
            chunks.append(current)
            current = []
        while len(group) > max_lines:
            chunks.append(group[:max_lines])
            group = group[max_lines:]
        current.extend(group)
    if current:
        chunks.append(current)
    normalized = ["\n".join(chunk).strip() for chunk in chunks if chunk]
    if len(normalized) <= 1:
        return (message,)
    return tuple(
        OutboundMessage(message_type="text", body=body)
        if index < len(normalized) - 1
        else OutboundMessage(
            message_type=message.message_type,
            body=body,
            interactive_id=message.interactive_id,
            outbound_payload=message.outbound_payload,
        )
        for index, body in enumerate(normalized)
    )


def _semantic_line_groups(body: str, width: int) -> list[list[str]]:
    groups: list[list[str]] = []
    for raw_line in body.splitlines() or [body]:
        normalized = " ".join(raw_line.split())
        if not normalized:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", normalized)
        for sentence in sentences:
            wrapped = textwrap.wrap(
                sentence,
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            if wrapped:
                groups.append(wrapped)
    return groups


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


def equipment_purchase_clarification_message(
    *,
    retry: bool = False,
) -> OutboundMessage:
    body = (
        "Só para eu direcionar corretamente: você quer comprar o aparelho, "
        "contratar a instalação ou os dois?"
        if not retry
        else (
            "Quero confirmar para não te direcionar errado. Você precisa somente "
            "da instalação, somente comprar o aparelho, ou quer compra e instalação?"
        )
    )
    return OutboundMessage(
        message_type="interactive_button",
        body=body,
        interactive_id="service.purchase_clarification",
        outbound_payload=_button_payload(
            (
                BookingOption(EQUIPMENT_INSTALLATION, "Só instalação"),
                BookingOption(EQUIPMENT_PURCHASE, "Comprar aparelho"),
                BookingOption(EQUIPMENT_BOTH, "Compra + instalação"),
            )
        ),
    )


def installation_equipment_status_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body=(
            "Você já tem o ar-condicionado e precisa só da instalação, "
            "ou também quer cotar o aparelho?"
        ),
        interactive_id="booking.equipment_status",
        outbound_payload=_button_payload(
            (
                BookingOption(EQUIPMENT_HAS, "Já tenho"),
                BookingOption(EQUIPMENT_NEEDS, "Quero cotar aparelho"),
            )
        ),
    )


def equipment_model_known_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="Você já tem algum modelo de ar-condicionado em mente?",
        interactive_id="booking.equipment_model_known",
        outbound_payload=_button_payload(
            (
                BookingOption(EQUIPMENT_MODEL_KNOWN, "Sim"),
                BookingOption(EQUIPMENT_MODEL_RECOMMEND, "Não"),
            )
        ),
    )


def equipment_model_request_message(
    body: str = "Qual é a marca e o modelo do ar-condicionado?",
) -> OutboundMessage:
    return OutboundMessage(message_type="text", body=body)


def equipment_profile_message(
    missing: Sequence[str],
    *,
    retry: bool = False,
) -> OutboundMessage:
    if not missing:
        return OutboundMessage(
            message_type="text",
            body="Perfeito. Já tenho as informações para sugerir um equipamento.",
        )

    field = missing[0]
    if field == "cycle":
        return OutboundMessage(
            message_type="interactive_button",
            body="Você prefere um aparelho somente frio ou quente e frio?",
            interactive_id="booking.equipment_cycle",
            outbound_payload=_button_payload(
                (
                    BookingOption(EQUIPMENT_CYCLE_COOLING, "Somente frio"),
                    BookingOption(EQUIPMENT_CYCLE_HEAT_COOL, "Quente e frio"),
                )
            ),
        )
    if field == "indoor_space":
        return OutboundMessage(
            message_type="interactive_button",
            body="Há espaço livre para instalar a unidade interna na parede?",
            interactive_id="booking.equipment_indoor_space",
            outbound_payload=_button_payload(
                (
                    BookingOption(EQUIPMENT_INDOOR_SPACE_YES, "Há espaço"),
                    BookingOption(EQUIPMENT_INDOOR_SPACE_NO, "Espaço limitado"),
                )
            ),
        )
    if field == "outdoor_space":
        return OutboundMessage(
            message_type="interactive_button",
            body="Há espaço ventilado e seguro para a unidade externa?",
            interactive_id="booking.equipment_outdoor_space",
            outbound_payload=_button_payload(
                (
                    BookingOption(EQUIPMENT_OUTDOOR_SPACE_YES, "Há espaço"),
                    BookingOption(EQUIPMENT_OUTDOOR_SPACE_NO, "Espaço limitado"),
                )
            ),
        )

    labels = {
        "people": "quantas pessoas, no máximo, costumam ficar no ambiente",
        "area": "qual é o tamanho aproximado do ambiente em m²",
        "preference": (
            "se você prefere tecnologia mais moderna, bom custo-benefício "
            "ou máxima economia na compra"
        ),
    }
    question = labels.get(field)
    if question is None:
        body = "Pode me contar um pouco mais sobre o ambiente?"
    else:
        body = (
            f"Só falta eu saber {question}."
            if not retry
            else f"Para eu fechar a recomendação, me diga {question}."
        )
    return OutboundMessage(message_type="text", body=body)


def unsupported_media_message(media_type: str) -> OutboundMessage:
    label = "vídeo" if media_type == "video" else "áudio"
    return OutboundMessage(
        message_type="interactive_button",
        body=(
            f"Recebi seu {label}, mas ainda não posso interpretar esse conteúdo.\n"
            "Você pode escrever a informação aqui.\n"
            "Se preferir uma pessoa, o atendimento pode demorar mais."
        ),
        interactive_id="media.unsupported",
        outbound_payload=_button_payload(
            (
                BookingOption(MEDIA_CONTINUE_TEXT, "Continuar por texto"),
                BookingOption(MEDIA_HUMAN_CONFIRM, "Falar com a equipe"),
            )
        ),
    )


def received_photo_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body=(
            "Recebi a foto e ela ficará disponível no atendimento.\n"
            "Como não faço diagnóstico só pela imagem, continue por texto."
        ),
    )


def equipment_photo_message(
    image_url: str,
    caption: str,
) -> OutboundMessage:
    return OutboundMessage(
        message_type="image",
        body=caption,
        outbound_payload={"link": image_url},
    )


def equipment_preference_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="Qual perfil faz mais sentido para você?",
        interactive_id="booking.equipment_preference",
        outbound_payload=_button_payload(
            (
                BookingOption(EQUIPMENT_PREF_MODERN, "Mais moderno"),
                BookingOption(EQUIPMENT_PREF_COST_BENEFIT, "Custo-benefício"),
                BookingOption(EQUIPMENT_PREF_ECONOMY, "Maior economia"),
            )
        ),
    )


def installation_height_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body=(
            "A unidade interna ou externa ficará instalada a mais de 3 metros "
            "de altura do piso?"
        ),
        interactive_id="booking.installation_height",
        outbound_payload=_button_payload(
            (
                BookingOption(HEIGHT_AT_MOST_3M, "Até 3 metros"),
                BookingOption(HEIGHT_OVER_3M, "Mais de 3 metros"),
            )
        ),
    )


def tubing_variation_message(
    included_meters: Decimal | None,
    extra_meter_price: Decimal | None,
) -> OutboundMessage:
    included = included_meters if included_meters is not None else Decimal("3")
    included_label = (
        str(int(included))
        if included == included.to_integral()
        else str(included).replace(".", ",")
    )
    body = (
        f"Importante: a instalação considera até {included_label} metros de "
        "tubulação. Se o comprimento necessário for maior, o valor pode variar."
    )
    if extra_meter_price is not None and extra_meter_price > 0:
        body = (
            f"Importante: a instalação considera até {included_label} metros de "
            f"tubulação. Acima disso, o valor cadastrado é "
            f"{_format_brl(extra_meter_price)} por metro adicional."
        )
    return OutboundMessage(message_type="text", body=body)


def property_type_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body="O atendimento será em casa, prédio ou condomínio?",
        interactive_id="booking.property_type",
        outbound_payload=_button_payload(
            (
                BookingOption(PROPERTY_HOUSE, "Casa"),
                BookingOption(PROPERTY_BUILDING, "Prédio"),
                BookingOption(PROPERTY_CONDOMINIUM, "Condomínio"),
            )
        ),
    )


def building_hours_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body=(
            "Qual é o horário permitido para entrada e trabalho de prestadores? "
            "Exemplo: das 08:00 às 17:00."
        ),
    )


def gate_details_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body=(
            "E o que devemos informar na portaria para liberar a entrada? "
            "Pode ser bloco, apartamento, torre ou nome do responsável."
        ),
    )


def attendee_message(customer_name: str | None) -> OutboundMessage:
    name = customer_name or "você"
    return OutboundMessage(
        message_type="interactive_button",
        body=f"No dia do serviço, é {name} quem vai receber o técnico no local?",
        interactive_id="booking.attendee",
        outbound_payload=_button_payload(
            (
                BookingOption(ATTENDEE_CUSTOMER, "Sim"),
                BookingOption(ATTENDEE_OTHER, "Outra pessoa"),
            )
        ),
    )


def attendee_name_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Qual é o nome da pessoa que vai receber o técnico?",
    )


def phone_confirmation_message(phone: str) -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body=(
            f"Se precisarmos falar sobre o atendimento, podemos ligar ou chamar "
            f"neste WhatsApp: {phone}?"
        ),
        interactive_id="booking.phone_confirmation",
        outbound_payload=_button_payload(
            (
                BookingOption(PHONE_CONFIRM, "Sim"),
                BookingOption(PHONE_OTHER, "Outro número"),
            )
        ),
    )


def phone_request_message() -> OutboundMessage:
    return OutboundMessage(
        message_type="text",
        body="Qual número devemos usar para contato no dia do atendimento?",
    )


def quote_decision_message(body: str) -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body=body,
        interactive_id="quote.decision",
        outbound_payload=_button_payload(
            (
                BookingOption(QUOTE_SCHEDULE, "Consultar agenda"),
                BookingOption(QUOTE_FINISH, "Só queria a cotação"),
            )
        ),
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


def address_city_confirmation_message(city: str) -> OutboundMessage:
    return OutboundMessage(
        message_type="interactive_button",
        body=f"Só preciso confirmar a cidade. Esse endereço fica em {city}?",
        interactive_id="booking.address_city",
        outbound_payload=_button_payload(
            (
                BookingOption(ADDRESS_CITY_CONFIRM, "Sim"),
                BookingOption(ADDRESS_CITY_OTHER, "Outra cidade"),
            )
        ),
    )


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
    safe_body = body
    if len(options) > MAX_LIST_ROWS:
        safe_body = (
            f"{body.rstrip()} "
            "Toque em “Ver opções” ou me diga o horário que prefere."
        )[:1024]
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
