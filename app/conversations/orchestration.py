from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.conversations.interpreter import (
    ConversationAct,
    ConversationIntent,
    Interpretation,
    normalize_portuguese,
)


class TurnRelation(StrEnum):
    """How an inbound message relates to the conversational checkpoint."""

    ANSWER = "answer"
    ANSWER_WITH_SIDE_QUESTION = "answer_with_side_question"
    SIDE_QUESTION = "side_question"
    REFERRAL = "referral"
    SOCIAL = "social"
    PAUSE = "pause"
    FAILURE = "failure"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class TurnOrchestration:
    relation: TurnRelation
    consumes_repair: bool


_REFERRAL_MARKERS = (
    "me indicou",
    "me indicaram",
    "indicou voces",
    "indicaram voces",
    "indicacao",
    "peguei seu numero com",
    "peguei o numero com",
    "peguei seu contato com",
    "peguei o contato com",
    "passou seu numero",
    "passou o numero",
    "passou seu contato",
    "passou o contato",
    "me passou o numero",
    "me passou seu numero",
    "me passou o contato",
    "me passou seu contato",
    "recomendou voces",
    "recomendou voce",
    "falou de voces",
    "falou de voce",
)


def is_referral_message(body: str | None) -> bool:
    normalized = normalize_portuguese(body or "")
    if not normalized:
        return False
    if any(marker in normalized for marker in _REFERRAL_MARKERS):
        return True
    return (
        "numero" in normalized or "contato" in normalized
    ) and any(
        source in normalized
        for source in (
            "irmao",
            "irma",
            "amigo",
            "amiga",
            "vizinho",
            "vizinha",
            "sindico",
            "sindica",
            "cliente",
            "colega",
            "mae",
            "pai",
            "primo",
            "prima",
        )
    ) and any(
        verb in normalized
        for verb in ("peguei", "passou", "mandou", "indicou", "recomendou")
    )


def referral_acknowledgement(customer_name: str | None = None) -> str:
    lead = f"{customer_name}, " if customer_name else ""
    return (
        f"Que legal, {lead}fico feliz pela indicação. "
        "Obrigado por chegar até a gente por recomendação."
    )


def classify_pending_turn(
    *,
    body: str | None,
    interpretation: Interpretation,
    can_answer_pending: bool,
    has_question_answer: bool,
    is_clarification: bool,
) -> TurnOrchestration:
    """Classify a turn before a state handler decides whether it is a failure.

    Only a genuine incompatible/unknown answer consumes the single recovery
    attempt. Social turns, referrals, pauses and side questions preserve the
    checkpoint without penalizing the customer.
    """

    if can_answer_pending:
        if interpretation.has_act(ConversationAct.SIDE_QUESTION):
            return TurnOrchestration(
                TurnRelation.ANSWER_WITH_SIDE_QUESTION,
                consumes_repair=False,
            )
        return TurnOrchestration(TurnRelation.ANSWER, consumes_repair=False)

    if is_referral_message(body):
        return TurnOrchestration(TurnRelation.REFERRAL, consumes_repair=False)

    if interpretation.has_act(ConversationAct.PAUSE):
        return TurnOrchestration(TurnRelation.PAUSE, consumes_repair=False)

    if interpretation.has_act(ConversationAct.SOCIAL):
        return TurnOrchestration(TurnRelation.SOCIAL, consumes_repair=False)

    if (
        interpretation.has_act(ConversationAct.SIDE_QUESTION)
        or has_question_answer
        or is_clarification
    ):
        return TurnOrchestration(TurnRelation.SIDE_QUESTION, consumes_repair=False)

    if interpretation.intent is ConversationIntent.UNKNOWN:
        return TurnOrchestration(TurnRelation.FAILURE, consumes_repair=True)

    return TurnOrchestration(TurnRelation.OTHER, consumes_repair=False)
