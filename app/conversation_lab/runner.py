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
    DISCOVERY_FLOW_CASES,
    DISCOVERY_JOURNEY_CASES,
    DISCOVERY_SEEDS,
    PREFIXES,
    REGRESSION_CONTEXT,
    REGRESSION_SEMANTIC,
    SUFFIXES,
    FlowCase,
    JourneyCase,
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
        "root": case.text,
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
    outbound = ""
    if repository.outbounds:
        outbound = repository.outbounds[-1].transition.outbound.body or ""
    missing_outbound_terms = [
        term
        for term in case.required_outbound_terms
        if term.casefold() not in outbound.casefold()
    ]
    automation_ok = (
        case.expected_automation_enabled is None
        or repository.automation_enabled == case.expected_automation_enabled
    )
    handoff_ok = (
        case.expected_handoff_status is None
        or repository.handoff_status == case.expected_handoff_status
    )
    passed = (
        not forbidden_present
        and not forbidden_calls_present
        and not missing_outbound_terms
        and automation_ok
        and handoff_ok
        and (case.expected_state is None or repository.state == case.expected_state)
    )
    return {
        "kind": "context",
        "family": case.family,
        "root": case.text,
        "input": text,
        "expected_state": case.expected_state,
        "actual_state": repository.state,
        "forbidden_present": forbidden_present,
        "forbidden_calls_present": forbidden_calls_present,
        "missing_outbound_terms": missing_outbound_terms,
        "expected_automation_enabled": case.expected_automation_enabled,
        "expected_handoff_status": case.expected_handoff_status,
        "handoff_status": repository.handoff_status,
        "automation_enabled": repository.automation_enabled,
        "outbound": outbound,
        "dry_run_calls": port.calls,
        "pass": passed,
    }


async def _flow_result(case: FlowCase, text: str, sequence: int) -> dict[str, object]:
    context = dict(case.context_items)
    port = DryRunBookingPort()
    repository = LabConversationRepository(state=case.state, context=context)
    engine = ConversationEngine(repository, port)

    await engine.process(inbound(sequence, body=text))
    side_state = repository.state
    side_outbound = ""
    if repository.outbounds:
        side_outbound = repository.outbounds[-1].transition.outbound.body or ""

    missing_side_terms = [
        term for term in case.side_contains if term.casefold() not in side_outbound.casefold()
    ]
    side_forbidden_calls = [
        call for call in case.forbidden_calls if call in port.calls
    ]
    side_passed = (
        (case.side_expected_state is None or side_state == case.side_expected_state)
        and not missing_side_terms
        and not side_forbidden_calls
        and repository.automation_enabled
        and repository.handoff_status == "none"
    )

    await engine.process(inbound(sequence + 1, body=case.followup_text))
    final_state = repository.state
    missing_context_keys = [
        key for key in case.followup_context_keys if key not in repository.context
    ]
    missing_required_calls = [
        call for call in case.required_calls if call not in port.calls
    ]
    forbidden_calls_present = [
        call for call in case.forbidden_calls if call in port.calls
    ]
    passed = (
        side_passed
        and (case.followup_expected_state is None or final_state == case.followup_expected_state)
        and not missing_context_keys
        and not missing_required_calls
        and not forbidden_calls_present
        and repository.automation_enabled
        and repository.handoff_status == "none"
    )
    final_outbound = ""
    if repository.outbounds:
        final_outbound = repository.outbounds[-1].transition.outbound.body or ""

    return {
        "kind": "flow",
        "family": case.family,
        "root": f"{case.side_text} -> {case.followup_text}",
        "input": f"{text} -> {case.followup_text}",
        "side_input": text,
        "followup_input": case.followup_text,
        "side_expected_state": case.side_expected_state,
        "side_actual_state": side_state,
        "followup_expected_state": case.followup_expected_state,
        "followup_actual_state": final_state,
        "missing_side_terms": missing_side_terms,
        "missing_context_keys": missing_context_keys,
        "missing_required_calls": missing_required_calls,
        "forbidden_calls_present": forbidden_calls_present,
        "side_outbound": side_outbound,
        "final_outbound": final_outbound,
        "dry_run_calls": port.calls,
        "pass": passed,
    }


def _journey_variants(case: JourneyCase, per_seed: int) -> list[JourneyCase]:
    variants: list[JourneyCase] = [case]
    seen = {
        tuple((step.body, step.action) for step in case.steps)
    }
    for index in case.mutate_steps:
        if index < 0 or index >= len(case.steps):
            continue
        step = case.steps[index]
        if not step.body:
            continue
        for body in _mutations(
            step.body,
            min(case.variants_per_step, per_seed),
        ):
            if body == step.body:
                continue
            steps = list(case.steps)
            steps[index] = replace(step, body=body)
            signature = tuple((item.body, item.action) for item in steps)
            if signature in seen:
                continue
            seen.add(signature)
            variants.append(replace(case, steps=tuple(steps)))
    return variants


async def _journey_result(case: JourneyCase, sequence: int) -> dict[str, object]:
    port = DryRunBookingPort()
    repository = LabConversationRepository()
    engine = ConversationEngine(repository, port)
    step_results: list[dict[str, object]] = []
    saw_handoff = False

    for index, step in enumerate(case.steps, start=1):
        await engine.process(
            inbound(
                sequence,
                body=step.body,
                action=step.action,
            )
        )
        sequence += 1

        outbound = ""
        if repository.outbounds:
            transition = repository.outbounds[-1].transition
            outbound = " ".join(
                message.body or ""
                for message in (transition.outbound, *transition.follow_ups)
            )

        missing_terms = [
            term
            for term in step.contains
            if term.casefold() not in outbound.casefold()
        ]
        missing_context_keys = [
            key
            for key in step.required_context_keys
            if key not in repository.context
        ]
        state_ok = (
            step.expected_state is None
            or repository.state == step.expected_state
        )
        if (
            repository.state == ConversationState.HUMAN_HANDOFF
            or repository.handoff_status != "none"
            or not repository.automation_enabled
        ):
            saw_handoff = True

        step_results.append(
            {
                "step": index,
                "body": step.body,
                "action": step.action,
                "expected_state": step.expected_state,
                "actual_state": repository.state,
                "missing_terms": missing_terms,
                "missing_context_keys": missing_context_keys,
                "outbound": outbound,
                "pass": state_ok and not missing_terms and not missing_context_keys,
            }
        )

    missing_required_calls = [
        call for call in case.required_calls if call not in port.calls
    ]
    forbidden_calls_present = [
        call for call in case.forbidden_calls if call in port.calls
    ]
    failed_steps = [item for item in step_results if not item["pass"]]
    passed = (
        not failed_steps
        and repository.state == case.expected_final_state
        and not missing_required_calls
        and not forbidden_calls_present
        and (case.allow_handoff or not saw_handoff)
    )

    return {
        "kind": "journey",
        "family": case.family,
        "root": case.family,
        "input": " -> ".join(
            step.body or step.action or ""
            for step in case.steps
        ),
        "expected_final_state": case.expected_final_state,
        "actual_final_state": repository.state,
        "missing_required_calls": missing_required_calls,
        "forbidden_calls_present": forbidden_calls_present,
        "saw_handoff": saw_handoff,
        "dry_run_calls": port.calls,
        "failed_steps": failed_steps,
        "pass": passed,
    }


async def run(mode: str, per_seed: int) -> int:
    if os.getenv("CONVERSATION_LAB_ALLOW_EXTERNAL_IO", "false").lower() == "true":
        raise RuntimeError("conversation_lab_external_io_must_remain_disabled")

    semantic_cases = list(REGRESSION_SEMANTIC)
    context_cases = list(REGRESSION_CONTEXT)
    flow_cases: list[FlowCase] = []
    journey_cases: list[JourneyCase] = []
    if mode in {"discovery", "all"}:
        semantic_cases.extend(DISCOVERY_SEEDS)
        context_cases.extend(DISCOVERY_CONTEXT_SEEDS)
        flow_cases.extend(DISCOVERY_FLOW_CASES)
        journey_cases.extend(DISCOVERY_JOURNEY_CASES)

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

    for case in flow_cases:
        variants = _mutations(case.side_text, per_seed)
        for text in variants:
            results.append(await _flow_result(case, text, sequence))
            sequence += 2

    for case in journey_cases:
        for journey_variant in _journey_variants(case, per_seed):
            results.append(await _journey_result(journey_variant, sequence))
            sequence += len(journey_variant.steps)

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
    semantic_roots = {
        str(item.get("root"))
        for item in results
        if item.get("kind") == "semantic"
    }
    context_roots = {
        str(item.get("root"))
        for item in results
        if item.get("kind") == "context"
    }
    flow_roots = {
        str(item.get("root"))
        for item in results
        if item.get("kind") == "flow"
    }
    journey_roots = {
        str(item.get("root"))
        for item in results
        if item.get("kind") == "journey"
    }
    states_covered = sorted(
        {
            str(item.get(key))
            for item in results
            for key in (
                "expected_state",
                "actual_state",
                "side_expected_state",
                "side_actual_state",
                "followup_expected_state",
                "followup_actual_state",
                "expected_final_state",
                "actual_final_state",
            )
            if item.get(key) is not None
        }
    )
    summary = {
        "mode": mode,
        "total": len(results),
        "passed": len(passed),
        "gaps": len(gaps),
        "families": sorted(family_totals),
        "semantic_roots": len(semantic_roots),
        "context_roots": len(context_roots),
        "flow_roots": len(flow_roots),
        "journey_roots": len(journey_roots),
        "states_covered": states_covered,
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
