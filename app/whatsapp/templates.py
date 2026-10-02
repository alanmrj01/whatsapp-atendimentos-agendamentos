from __future__ import annotations

RESCHEDULE_TEMPLATE_LANGUAGE = "pt_BR"
RESCHEDULE_TEMPLATE_NAME = "alovia_reagendamento_atendimento"
RESCHEDULE_PREFERRED_TEMPLATE_NAME = "alovia_reagendamento_preferencia"


def render_reschedule_template_preview(
    *,
    customer_name: str,
    service_name: str,
    current_slot: str,
) -> str:
    return (
        f"Olá, {customer_name}. Precisamos reagendar seu atendimento de "
        f"{service_name}, atualmente marcado para {current_slot}. "
        "Responda a esta mensagem para escolher uma nova data e horário disponíveis."
    )


def render_reschedule_preferred_template_preview(
    *,
    customer_name: str,
    service_name: str,
    current_slot: str,
    preferred_slot: str,
) -> str:
    return (
        f"Olá, {customer_name}. Precisamos reagendar seu atendimento de "
        f"{service_name}, atualmente marcado para {current_slot}. "
        f"Podemos transferir para {preferred_slot}? "
        "Responda SIM para confirmar ou responda OUTRO HORÁRIO para ver outras opções."
    )
