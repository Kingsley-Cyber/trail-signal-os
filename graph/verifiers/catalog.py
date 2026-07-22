"""Verifier catalog registry (doc 07 §4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from graph.verifiers.claim_grounding import claim_grounding
from graph.verifiers.decision_valid import decision_valid
from graph.verifiers.novelty_floor import novelty_floor
from graph.verifiers.plan_validates import plan_validates
from graph.verifiers.quorum_met import quorum_met
from graph.verifiers.sample_judge import sample_judge
from graph.verifiers.schema_validate import schema_validate

VerifierFactory = Callable[..., Any]


@dataclass(frozen=True)
class VerifierSpec:
    name: str
    kind: str
    used_by: str
    factory: VerifierFactory


VERIFIER_CATALOG: dict[str, VerifierSpec] = {
    "schema_validate": VerifierSpec(
        name="schema_validate",
        kind="deterministic",
        used_by="ENRICH, all typed outputs",
        factory=schema_validate,
    ),
    "plan_validates": VerifierSpec(
        name="plan_validates",
        kind="deterministic",
        used_by="PLANNER",
        factory=plan_validates,
    ),
    "claim_grounding": VerifierSpec(
        name="claim_grounding",
        kind="deterministic",
        used_by="REVIEWER",
        factory=claim_grounding,
    ),
    "quorum_met": VerifierSpec(
        name="quorum_met",
        kind="deterministic",
        used_by="fan-in edge",
        factory=quorum_met,
    ),
    "novelty_floor": VerifierSpec(
        name="novelty_floor",
        kind="deterministic",
        used_by="expand back-edge",
        factory=novelty_floor,
    ),
    "decision_valid": VerifierSpec(
        name="decision_valid",
        kind="deterministic",
        used_by="GAP ANALYST",
        factory=decision_valid,
    ),
    "sample_judge": VerifierSpec(
        name="sample_judge",
        kind="llm",
        used_by="optional QA on ENRICH",
        factory=sample_judge,
    ),
}

CATALOG_VERIFIER_NAMES = tuple(VERIFIER_CATALOG.keys())


def list_verifiers() -> tuple[str, ...]:
    return CATALOG_VERIFIER_NAMES


def get_verifier_factory(name: str) -> VerifierFactory:
    try:
        return VERIFIER_CATALOG[name].factory
    except KeyError as exc:
        raise KeyError(f"unknown verifier {name!r}") from exc
