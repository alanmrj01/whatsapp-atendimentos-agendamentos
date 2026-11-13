from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import unicodedata
from collections import Counter
from dataclasses import asdict, replace
from itertools import islice

from app.booking.domain import PricingType
from app.conversation_lab.adapters import DryRunBookingPort, LabConversationRepository, SERVICE_ID, inbound
from app.conversation_lab.corpus import (
    ContextCase,
    DISCOVERY_CONTEXT_SEEDS,
    DISCOVERY_SEEDS,
    PREFIXES,
    REGRESSION_CONTEXT,
    REGRESSION_SEMANTIC,
    SUFFIXES,
    SemanticCase,
    TYPO_REPLACEMENTS,
)
from app.conversations.constants import ConversationState
from app.conversations.engine import ConversationEngine
from app.conversations.interpreter import DeterministicConversationInterpreter


def _strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


def _has_social_prefix(text: str) -> bool:
    normalized = text.strip().casefold()
    return any(
        prefix and normalized.startswith(prefix.strip().casefold())
        for prefix in PREFIXES
    )


def _has_politeness_suffix(text: str) -> bool:
    normalized = text.strip().casefold().rstrip("?!., ")
    return normalized.endswith(" por favor") or normalized.endswith(" pfv")


def _is_social_or_pause_message(text: str) -> bool:
    normalized = _strip_accents(text).strip().casefold().rstrip("?!., ")
    return any(
        marker in normalized
        for marker in (
            "valeu",
            "obrigado",
            "obrigada",
            "beleza",
            "blz",
            "pera ai",
            "um minutinho",
            "um minuto",
            "ja volto",
            "so um instante",
            "deixa eu ver",
        )
    )


def _mutations(text: str, limit: int) -> list[str]:
    candidates: list[str] = [text, text.lower(), text.upper(), _strip_accents(text)]
    lower = text.lower()
    for source, replacements in TYPO_REPLACEMENTS.items():
        if source in lower:
            for replacement in replacements:
                candidates.append(lower.replace(source, replacement))
    base = list(dict.fromkeys(candidates))
    expanded: list[str] = []
    for item in base:
        prefixes = ("",) if _has_social_prefix(item) else PREFIXES
        suffixes = tuple(
            suffix
            for suffix in SUFFIXES
            if not (
                (
                    suffix.strip().casefold() in {"por favor", "pfv"}
                    and _has_politeness_suffix(item)
                )
                or (
                    suffix.strip().casefold() in {"valeu", "blz"}
                    and _is_social_or_pause_message(item)
                )
            )
        )
        for prefix in prefixes:
            for suffix in suffixes:
                expanded.append(f"{prefix}{item}{suffix}".strip())
    return list(islice(dict.fromkeys(expanded), limit))


def _semantic_result(case: SemanticCase, text: str) -> dict[str, object]:
    result = DeterministicConversationInterpreter().interpret(text)
    forbidden_intents_present = [
        intent.value for intent in case.forbidden_intents if result.has(intent)
    ]
    forbidden_acts_present = [
        act.value for act in case.forbidden_acts if result.has_act(act)
    ]
    passed = (
        (case.intent is None or result.has(case.intent))
        and (case.act is None or result.has_act(case.act))
        and not forbidden_intents_present
        and not forbidden_acts_present
    )
    return {
        "kind": "semantic",
        "family": case.family,
        "input": text,
        "expected_intent": case.intent.value if case.intent else None,
        "expected_act": case.act.value if case.act else None,
        "pass": passed,
        "actual_intents": sorted(intent.value for intent in result.intents),
        "actual_acts": sorted(act.value for act in result.acts),
        "forbidden_intents_present": forbidden_intents_present,
        "forbidden_acts_present": forbidden_acts_present,
    }


async def _context_result(case: ContextCase, text: str, sequence: int) -> dict[str, object]:
    context: dict[str, object] = {"service_id": str(SERVICE_ID)}
    port = DryRunBookingPort()
    if case.state == ConversationState.BOOKING_TIME:
        context.update({
            "selected_date": "2026-11-16",
            "service_address": "Rua Sintética, 10, Centro",
            "quantity": 1,
        })
    if case.state == ConversationState.BOOKING_ADDRESS:
        port.intake = replace(port.intake, requires_address=True)
    if case.state == ConversationState.BOOKING_QUANTITY:
        port.intake = replace(port.intake, requires_quantity=True, pricing_type=PricingType.ESTIMATED)

    repository = LabConversationRepository(state=case.state, context=context)
    await ConversationEngine(repository, port).process(inbound(sequence, body=text))
    forbidden_present = [key for key in case.forbidden_context_keys if key in repository.context]
    forbidden_calls_present = [
        call for call in case.forbidden_calls if call in port.calls
    ]
    passed = (
        not forbidden_present
        and not forbidden_calls_present
        and (case.expected_state is None or repository.state == case.expected_state)
    )
    outbound = ""
    if repository.outbounds:
        outbound = repository.outbounds[-1].transition.outbound.body or ""
    return {
        "kind": "context",
        "family": case.family,
        "input": text,
        "expected_state": case.expected_state,
        "actual_state": repository.state,
        "forbidden_present": forbidden_present,
        "forbidden_calls_present": forbidden_calls_present,
        "handoff_status": repository.handoff_status,
        "automation_enabled": repository.automation_enabled,
        "outbound": outbound,
        "dry_run_calls": port.calls,
        "pass": passed,
    }


async def run(mode: str, per_seed: int) -> int:
    if os.getenv("CONVERSATION_LAB_ALLOW_EXTERNAL_IO", "false").lower() == "true":
        raise RuntimeError("conversation_lab_external_io_must_remain_disabled")

    semantic_cases = list(REGRESSION_SEMANTIC)
    context_cases = list(REGRESSION_CONTEXT)
    if mode in {"discovery", "all"}:
        semantic_cases.extend(DISCOVERY_SEEDS)
        context_cases.extend(DISCOVERY_CONTEXT_SEEDS)

    results: list[dict[str, object]] = []
    sequence = 1
    for case in semantic_cases:
        variants = [case.text] if mode == "regression" else _mutations(case.text, per_seed)
        for text in variants:
            results.append(_semantic_result(case, text))

    for case in context_cases:
        variants = [case.text] if mode == "regression" else _mutations(case.text, per_seed)
        for text in variants:
            results.append(await _context_result(case, text, sequence))
            sequence += 1

    gaps = [item for item in results if not item["pass"]]
    passed = [item for item in results if item["pass"]]
    family_totals = Counter(str(item["family"]) for item in results)
    family_passed = Counter(str(item["family"]) for item in passed)
    family_gaps = Counter(str(item["family"]) for item in gaps)
    fingerprint_payload = "\n".join(
        sorted(
            f'{item["kind"]}|{item["family"]}|{item["input"]}|{item["pass"]}'
            for item in results
        )
    )
    summary = {
        "mode": mode,
        "total": len(results),
        "passed": len(passed),
        "gaps": len(gaps),
        "families": sorted(family_totals),
        "fingerprint": hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
    }
    print("CONVERSATION_LAB_SUMMARY " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    for family in sorted(family_totals):
        print(
            "CONVERSATION_LAB_FAMILY "
            + json.dumps(
                {
                    "family": family,
                    "total": family_totals[family],
                    "passed": family_passed[family],
                    "gaps": family_gaps[family],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    for item in gaps:
        print("CONVERSATION_LAB_GAP " + json.dumps(item, ensure_ascii=False, sort_keys=True))
    if (
        mode in {"discovery", "all"}
        and os.getenv("CONVERSATION_LAB_EMIT_CANDIDATES", "false").lower() == "true"
    ):
        for item in passed:
            print("CONVERSATION_LAB_REGRESSION_CANDIDATE " + json.dumps(item, ensure_ascii=False, sort_keys=True))
    return 1 if mode == "regression" and gaps else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Alovia Conversation Lab")
    parser.add_argument("--mode", choices=("regression", "discovery", "all"), default=os.getenv("CONVERSATION_LAB_MODE", "regression"))
    parser.add_argument("--per-seed", type=int, default=int(os.getenv("CONVERSATION_LAB_PER_SEED", "120")))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.mode, max(1, min(args.per_seed, 500)))))


if __name__ == "__main__":
    main()
