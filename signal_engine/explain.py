"""LLM opportunity explanation — narrates precomputed scores, never computes them (N27, LAW 1)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from graph.verifiers.base import VerifierFn, VerifierResult
from guards.schema_guards import guard5_reject_llm_score_provenance
from harness.gateway import GatewayMode, LLMGateway
from signal_engine.score import validate_opportunity_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / "prompts" / "explain_opportunity.md"

EXPLAIN_ROLE = "enrich.primary"
CASSETTE_KIND = "explain"
PROMPT_VERSION = "explain_opportunity-2026.07.21"
CODE_VERSION = "explain-1.0.0"
OUTPUT_SCHEMA = "explanation"
MAX_ITERATIONS = 2

CASSETTE_MODEL_ID = "qwen3-4b-q4"

LAW1_FORBIDDEN_KEYS = frozenset(
    {
        "score",
        "subscores",
        "normalized_score",
        "opp_confidence",
        "opportunity_id",
        "final",
        "confidence",
    }
)


class ExplainError(Exception):
    """Opportunity explanation failed validation."""


@dataclass(frozen=True)
class ExplainExecutionResult:
    verdict: str
    attempts: int
    output: dict[str, Any] | None
    violations: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True)
class ExplainSuccess:
    explanation: dict[str, Any]
    opportunity: dict[str, Any]
    attempts: int
    replayed: bool


def load_explain_prompt() -> str:
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"missing explain prompt {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_replay_request(
    opportunity: Mapping[str, Any],
    *,
    model_id: str = CASSETTE_MODEL_ID,
) -> dict[str, Any]:
    """Build cassette replay key fields for offline explain cassettes."""
    return {
        "role": EXPLAIN_ROLE,
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "opportunity_id": opportunity["opportunity_id"],
    }


def build_evidence_store(evidence_items: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index evidence.v1 rows by record_id for grounding checks."""
    store: dict[str, dict[str, Any]] = {}
    for item in evidence_items:
        record_id = item.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ExplainError("evidence item missing record_id")
        store[record_id] = dict(item)
    return store


def _validate_explanation_raw(
    raw: dict[str, Any],
    *,
    evidence_store: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str, ...]:
    violations: list[str] = []
    forbidden = sorted(LAW1_FORBIDDEN_KEYS.intersection(raw.keys()))
    if forbidden:
        violations.append(
            "LAW 1: explain output must not contain scoring fields: "
            + ", ".join(forbidden)
        )

    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        violations.append("explanation.text must be a non-empty string")

    cited_record_ids = raw.get("cited_record_ids")
    if cited_record_ids is not None and not isinstance(cited_record_ids, list):
        violations.append("explanation.cited_record_ids must be an array when present")
    elif isinstance(cited_record_ids, list):
        for record_id in cited_record_ids:
            if not isinstance(record_id, str) or not record_id:
                violations.append("explanation.cited_record_ids must contain non-empty strings")
            elif evidence_store is not None and record_id not in evidence_store:
                violations.append(f"cited unknown record_id {record_id!r}")

    return tuple(violations)


def validate_explanation_output(
    raw: dict[str, Any],
    *,
    evidence_store: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    violations = _validate_explanation_raw(raw, evidence_store=evidence_store)
    if violations:
        raise ExplainError("; ".join(violations))


def finalize_explanation(
    raw: dict[str, Any],
    *,
    model_id: str,
    evidence_store: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    validate_explanation_output(raw, evidence_store=evidence_store)
    explanation = {
        "text": str(raw["text"]).strip(),
        "cited_record_ids": list(raw.get("cited_record_ids") or []),
        "provenance": {
            "model_id": model_id,
            "prompt_version": PROMPT_VERSION,
        },
    }
    return explanation


def explain_verifier(
    evidence_store: Mapping[str, Mapping[str, Any]] | None = None,
) -> VerifierFn:
    """Verifier: explanation prose only; cites grounded record_ids; never scores."""

    def _verify(output: dict[str, Any], _packed_input: dict[str, Any]) -> VerifierResult:
        store = evidence_store
        if store is None and isinstance(_packed_input.get("evidence_store"), dict):
            store = _packed_input["evidence_store"]
        violations = list(_validate_explanation_raw(output, evidence_store=store))
        return VerifierResult(passed=not violations, violations=tuple(violations))

    return _verify


def _score_fields_unchanged(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[str, ...]:
    violations: list[str] = []
    for key in ("score", "subscores", "confidence", "scored_from", "provenance"):
        if before.get(key) != after.get(key):
            violations.append(f"explain must not mutate opportunity.{key}")
    return tuple(violations)


def attach_explanation(
    opportunity: Mapping[str, Any],
    explanation: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach LLM explanation without mutating deterministic score fields."""
    guard5_reject_llm_score_provenance(dict(opportunity))
    validate_opportunity_v1(dict(opportunity))
    updated = copy.deepcopy(opportunity)
    updated["explanation"] = {
        "text": explanation["text"],
        "cited_record_ids": list(explanation.get("cited_record_ids") or []),
        "provenance": dict(explanation["provenance"]),
    }
    violations = _score_fields_unchanged(opportunity, updated)
    if violations:
        raise ExplainError("; ".join(violations))
    guard5_reject_llm_score_provenance(updated)
    validate_opportunity_v1(updated)
    return updated


def _build_messages(
    opportunity: Mapping[str, Any],
    evidence_store: Mapping[str, Mapping[str, Any]],
    *,
    prior_output: dict[str, Any] | None = None,
    violations: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    prompt = load_explain_prompt()
    user_content = {
        "node_id": "explain_opportunity",
        "input_schema": "opportunity.v1",
        "output_schema": OUTPUT_SCHEMA,
        "packed_input": {
            "opportunity": dict(opportunity),
            "evidence_store": {key: dict(value) for key, value in evidence_store.items()},
        },
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(user_content, sort_keys=True)},
    ]
    if prior_output is not None and violations:
        messages.append(
            {"role": "assistant", "content": json.dumps(prior_output, sort_keys=True)}
        )
        messages.append(
            {
                "role": "user",
                "content": "Repair the output. Verifier violations:\n"
                + "\n".join(f"- {item}" for item in violations),
            }
        )
    return messages


def _extract_llm_output(result_parsed: dict[str, Any] | None, text: str) -> dict[str, Any]:
    if isinstance(result_parsed, dict):
        return dict(result_parsed)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ExplainError("LLM output must be a JSON object")
    return payload


def explain_opportunity(
    opportunity: Mapping[str, Any],
    evidence_items: Sequence[Mapping[str, Any]],
    *,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    model_id: str = CASSETTE_MODEL_ID,
    max_iterations: int = MAX_ITERATIONS,
) -> ExplainExecutionResult:
    """Run explain loop via gateway cassette replay; never mutates opportunity scores."""
    validate_opportunity_v1(dict(opportunity))
    evidence_store = build_evidence_store(evidence_items)
    request = replay_request or build_replay_request(opportunity, model_id=model_id)
    llm_gateway = gateway or LLMGateway(mode=GatewayMode.REPLAY)
    verify = explain_verifier(evidence_store)

    last_violations: tuple[str, ...] = ()
    last_output: dict[str, Any] | None = None
    replayed = False

    packed_input = {
        "opportunity": dict(opportunity),
        "evidence_store": evidence_store,
    }

    for attempt in range(1, max_iterations + 1):
        messages = _build_messages(
            opportunity,
            evidence_store,
            prior_output=last_output if attempt > 1 else None,
            violations=last_violations if attempt > 1 else (),
        )
        completion = llm_gateway.generate(
            EXPLAIN_ROLE,
            messages,
            cassette_kind=CASSETTE_KIND,
            replay_request=request,
        )
        replayed = completion.replayed
        candidate = _extract_llm_output(completion.parsed, completion.text)
        result = verify(candidate, packed_input)
        if result.passed:
            return ExplainExecutionResult(
                verdict="pass",
                attempts=attempt,
                output=candidate,
                violations=(),
                replayed=replayed,
            )
        last_output = candidate
        last_violations = result.violations

    return ExplainExecutionResult(
        verdict="ceiling",
        attempts=max_iterations,
        output=last_output,
        violations=last_violations,
        replayed=replayed,
    )


def run_explain_task(
    opportunity: Mapping[str, Any],
    evidence_items: Sequence[Mapping[str, Any]],
    *,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    model_id: str = CASSETTE_MODEL_ID,
) -> ExplainSuccess:
    """Explain via LLM cassette and attach rationale without changing score fields."""
    llm_gateway = gateway or LLMGateway(mode=GatewayMode.REPLAY)
    role_model_id = llm_gateway.resolve_role(EXPLAIN_ROLE).model_id
    execution = explain_opportunity(
        opportunity,
        evidence_items,
        gateway=llm_gateway,
        replay_request=replay_request,
        model_id=role_model_id if llm_gateway.mode is GatewayMode.LIVE else CASSETTE_MODEL_ID,
    )
    if execution.verdict != "pass" or execution.output is None:
        raise ExplainError(
            "explain failed: "
            + ("; ".join(execution.violations) if execution.violations else execution.verdict)
        )

    explanation = finalize_explanation(
        execution.output,
        model_id=role_model_id,
        evidence_store=build_evidence_store(evidence_items),
    )
    updated = attach_explanation(opportunity, explanation)
    return ExplainSuccess(
        explanation=explanation,
        opportunity=updated,
        attempts=execution.attempts,
        replayed=bool(execution.replayed),
    )


def assert_law1_explain_output(raw: dict[str, Any]) -> None:
    """Prove LAW 1 split: explain emits prose + citations only, never scores."""
    validate_explanation_output(raw)


def explain_camping_fixture(
    *,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
) -> ExplainSuccess:
    """Score camping-fixture and attach cassette-backed explanation."""
    from fixtures.load import load_fixtures
    from signal_engine.score import score_camping_fixture

    corpus = load_fixtures()
    opportunity = score_camping_fixture()
    evidence_items = [_camping_pain_evidence(corpus)]
    for record_id in ("ev_camping_pain_1108", "ev_camping_pain_1155"):
        evidence_items.append(
            {
                "record_id": record_id,
                "observation": "Additional pain-theme evidence for camping fan complaints.",
                "schema_version": "evidence.v1",
            }
        )
    cassette_request = replay_request
    if cassette_request is None:
        cassette = corpus.cassettes[CASSETTE_KIND][0]
        cassette_request = dict(cassette["request"])
    return run_explain_task(
        opportunity,
        evidence_items,
        gateway=gateway,
        replay_request=cassette_request,
    )


def _camping_pain_evidence(corpus) -> dict[str, Any]:
    return dict(corpus.cassettes["enrich"][0]["response"]["parsed"])


__all__ = [
    "CASSETTE_KIND",
    "CASSETTE_MODEL_ID",
    "CODE_VERSION",
    "EXPLAIN_ROLE",
    "ExplainError",
    "ExplainExecutionResult",
    "ExplainSuccess",
    "PROMPT_VERSION",
    "assert_law1_explain_output",
    "attach_explanation",
    "build_evidence_store",
    "build_replay_request",
    "explain_camping_fixture",
    "explain_opportunity",
    "explain_verifier",
    "finalize_explanation",
    "load_explain_prompt",
    "run_explain_task",
    "validate_explanation_output",
]
