"""Constraint-fit re-rank (deterministic) + LLM rationale split (N30, LAW 1)."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import psycopg
import yaml
from jsonschema import Draft202012Validator

from graph.verifiers.base import VerifierFn, VerifierResult
from harness.gateway import GatewayMode, LLMGateway
from signal_engine.score import validate_opportunity_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONSTRAINTS_PATH = REPO_ROOT / "config" / "constraints.yaml"
SCHEMAS_DIR = REPO_ROOT / "schemas"

DECIDE_ROLE = "reason.primary"
CASSETTE_KIND = "decide"
PROMPT_VERSION = "decide_rationale-2026.07.21"
CODE_VERSION = "decide-1.0.0"
DECISION_SCHEMA_VERSION = "decision.v1"
OUTPUT_SCHEMA = "decision_rationale"
MAX_ITERATIONS = 2

CASSETTE_MODEL_ID = "qwen3-4b-q4"

CONSTRAINT_AXES = ("margin_potential", "shipping_fit", "channel_fit")
DEFAULT_CHANNEL_FIELD = "community_reachability"

DECIDE_PROMPT = """# Decide Rationale — narrates a deterministic decision (LAW 1)

You explain a precomputed graph routing decision. The action, constraint ranks, and scores are already chosen — you narrate them; you never choose or change them.

## Role

- Read the packed `ranked_opportunities`, `selected_action`, and `selected_args` in the user message.
- Emit one rationale JSON object — nothing else.
- Do **not** output `action`, `args`, `constraint_rank`, `score`, `subscores`, `fit_score`, `rank`, or any numeric ranking fields. (LAW 1)
- Do **not** invent metrics, demand, prices, or quotations not supported by cited evidence.

## Output contract

Return JSON with at minimum:

- `text` — natural-language rationale referencing the deterministic action and constraint-fit outcome as given in the input (do not recompute ranks or scores)
- `cited_record_ids` — array of `ev_*` record ids supporting factual claims in the text (may be empty when none are provided)

## Repair

If verifier feedback is provided, fix only the cited schema, grounding, or LAW 1 violations. Do not add actions, ranks, or unsupported claims.
"""

LAW1_FORBIDDEN_KEYS = frozenset(
    {
        "action",
        "args",
        "constraint_rank",
        "score",
        "subscores",
        "fit_score",
        "rank",
        "ranked_opportunity_ids",
        "decision_id",
        "normalized_score",
        "confidence",
        "opportunity_id",
        "final",
    }
)


class DecideError(Exception):
    """Decision task failed validation."""


@dataclass(frozen=True)
class ConstraintFit:
    fit_score: float
    margin_score: float
    shipping_score: float
    channel_score: float
    passes: bool
    profile: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fit_score": self.fit_score,
            "margin_score": self.margin_score,
            "shipping_score": self.shipping_score,
            "channel_score": self.channel_score,
            "passes": self.passes,
            "profile": dict(self.profile),
        }


@dataclass(frozen=True)
class RankedOpportunity:
    opportunity: dict[str, Any]
    constraint_fit: ConstraintFit
    combined_rank_score: float
    constraint_rank: int


@dataclass(frozen=True)
class RerankResult:
    ranked: tuple[RankedOpportunity, ...]
    constraints_version: str
    config_hash: str


@dataclass(frozen=True)
class DecideExecutionResult:
    verdict: str
    attempts: int
    output: dict[str, Any] | None
    violations: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True)
class DecideSuccess:
    decision: dict[str, Any]
    rerank: RerankResult
    attempts: int
    replayed: bool
    lineage_edges_written: int


def load_constraints(path: Path | None = None) -> dict[str, Any]:
    constraints_path = path or DEFAULT_CONSTRAINTS_PATH
    if not constraints_path.is_file():
        raise DecideError(f"constraints file not found: {constraints_path}")
    payload = yaml.safe_load(constraints_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DecideError(f"constraints file must contain a mapping: {constraints_path}")
    _validate_constraints(payload)
    return payload


def constraints_config_hash(constraints: Mapping[str, Any]) -> str:
    canonical = json.dumps(constraints, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _validate_constraints(constraints: Mapping[str, Any]) -> None:
    version = constraints.get("version")
    if not isinstance(version, str) or not version:
        raise DecideError("constraints.version must be a non-empty string")
    rerank = constraints.get("constraint_rerank")
    if not isinstance(rerank, Mapping):
        raise DecideError("constraints.constraint_rerank must be a mapping")
    axis_weights = rerank.get("axis_weights")
    if not isinstance(axis_weights, Mapping):
        raise DecideError("constraint_rerank.axis_weights must be a mapping")
    weight_sum = sum(float(axis_weights[key]) for key in CONSTRAINT_AXES if key in axis_weights)
    if not math.isclose(weight_sum, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise DecideError("constraint_rerank.axis_weights must sum to 1")


def _load_decision_schema() -> dict[str, Any]:
    path = SCHEMAS_DIR / "decision.v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_decision_v1(decision: dict[str, Any]) -> None:
    Draft202012Validator(_load_decision_schema()).validate(decision)


def _normalize_axis(value: Any) -> float:
    numeric = float(value)
    if numeric < 0 or numeric > 5:
        raise DecideError("constraint axis values must be within 0..5")
    return numeric / 5.0


def _resolve_profile(
    opportunity: Mapping[str, Any],
    candidate_profiles: Mapping[str, Mapping[str, Any]] | None,
    constraints: Mapping[str, Any],
) -> dict[str, int]:
    candidate = opportunity.get("candidate")
    if not isinstance(candidate, Mapping):
        raise DecideError("opportunity.candidate is required")
    candidate_id = candidate.get("candidate_id")
    profile: dict[str, Any] = {}
    if isinstance(candidate.get("constraint_profile"), Mapping):
        profile.update(candidate["constraint_profile"])
    if candidate_profiles and isinstance(candidate_id, str):
        external = candidate_profiles.get(candidate_id)
        if isinstance(external, Mapping):
            profile.update(external)
    rerank = constraints["constraint_rerank"]
    defaults = rerank.get("default_profile")
    if isinstance(defaults, Mapping):
        for key, value in defaults.items():
            profile.setdefault(key, value)
    channel_field = str(rerank.get("channel_fit_field", DEFAULT_CHANNEL_FIELD))
    if "channel_fit" not in profile and channel_field in profile:
        profile["channel_fit"] = profile[channel_field]
    resolved: dict[str, int] = {}
    for axis in ("margin_potential", "shipping_fit"):
        if axis not in profile:
            raise DecideError(f"constraint profile missing {axis} for {candidate_id!r}")
        resolved[axis] = int(profile[axis])
    if "channel_fit" not in profile:
        raise DecideError(f"constraint profile missing channel_fit for {candidate_id!r}")
    resolved["channel_fit"] = int(profile["channel_fit"])
    return resolved


def _weighted_geometric_mean(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    total_weight = sum(float(weights[key]) for key in weights)
    if total_weight <= 0:
        raise DecideError("constraint axis weights must be positive")
    log_sum = 0.0
    for key, weight in weights.items():
        value = float(values[key])
        if value <= 0:
            raise DecideError(f"constraint axis {key} must be positive after normalization")
        log_sum += float(weight) * math.log(value)
    return math.exp(log_sum / total_weight)


def compute_constraint_fit(
    opportunity: Mapping[str, Any],
    *,
    candidate_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    constraints: Mapping[str, Any] | None = None,
) -> ConstraintFit:
    """Deterministic constraint-fit evaluation for one opportunity."""
    active_constraints = constraints or load_constraints()
    profile = _resolve_profile(opportunity, candidate_profiles, active_constraints)
    rerank = active_constraints["constraint_rerank"]
    axis_weights = {
        key: float(rerank["axis_weights"][key])
        for key in CONSTRAINT_AXES
    }
    normalized = {
        "margin_potential": _normalize_axis(profile["margin_potential"]),
        "shipping_fit": _normalize_axis(profile["shipping_fit"]),
        "channel_fit": _normalize_axis(profile["channel_fit"]),
    }
    fit_score = round(
        _weighted_geometric_mean(normalized, axis_weights),
        4,
    )
    min_fit = float(rerank.get("min_fit_score", 0.55))
    return ConstraintFit(
        fit_score=fit_score,
        margin_score=round(normalized["margin_potential"], 4),
        shipping_score=round(normalized["shipping_fit"], 4),
        channel_score=round(normalized["channel_fit"], 4),
        passes=fit_score >= min_fit,
        profile=profile,
    )


def _combined_rank_score(
    opportunity: Mapping[str, Any],
    constraint_fit: ConstraintFit,
    constraints: Mapping[str, Any],
) -> float:
    rerank = constraints["constraint_rerank"]
    blend = rerank.get("rank_blend")
    if not isinstance(blend, Mapping):
        raise DecideError("constraint_rerank.rank_blend must be a mapping")
    opp_weight = float(blend.get("opportunity_weight", 0.65))
    constraint_weight = float(blend.get("constraint_weight", 0.35))
    if not math.isclose(opp_weight + constraint_weight, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise DecideError("rank_blend weights must sum to 1")
    score = float(opportunity["score"])
    return round((opp_weight * score) + (constraint_weight * constraint_fit.fit_score), 6)


def rerank_opportunities(
    opportunities: Sequence[Mapping[str, Any]],
    *,
    candidate_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    constraints: Mapping[str, Any] | None = None,
) -> RerankResult:
    """Deterministic constraint-fit re-rank over scored opportunities."""
    if not opportunities:
        raise DecideError("at least one opportunity is required")
    active_constraints = constraints or load_constraints()
    config_hash = constraints_config_hash(active_constraints)
    rerank_version = str(active_constraints["constraint_rerank"]["version"])

    scored_rows: list[tuple[dict[str, Any], ConstraintFit, float]] = []
    for opportunity in opportunities:
        payload = dict(opportunity)
        validate_opportunity_v1(payload)
        fit = compute_constraint_fit(
            payload,
            candidate_profiles=candidate_profiles,
            constraints=active_constraints,
        )
        combined = _combined_rank_score(payload, fit, active_constraints)
        scored_rows.append((payload, fit, combined))

    scored_rows.sort(
        key=lambda row: (
            -row[2],
            -float(row[0]["score"]),
            str(row[0]["opportunity_id"]),
        )
    )

    ranked: list[RankedOpportunity] = []
    for index, (opportunity, fit, combined) in enumerate(scored_rows, start=1):
        updated = copy.deepcopy(opportunity)
        updated["constraint_fit"] = fit.as_dict()
        ranked.append(
            RankedOpportunity(
                opportunity=updated,
                constraint_fit=fit,
                combined_rank_score=combined,
                constraint_rank=index,
            )
        )
    return RerankResult(
        ranked=tuple(ranked),
        constraints_version=rerank_version,
        config_hash=config_hash,
    )


def interpret_score_band(score: float, constraints: Mapping[str, Any]) -> dict[str, Any]:
    """Map opportunity score ∈ [0,1] to salvaged interpretation bands."""
    score_pct = float(score) * 100.0
    bands = constraints.get("score_interpretation_bands")
    if not isinstance(bands, Mapping):
        raise DecideError("score_interpretation_bands must be a mapping")
    reject = bands.get("reject_or_archive")
    if isinstance(reject, Mapping):
        max_exclusive = reject.get("max_exclusive")
        if isinstance(max_exclusive, (int, float)) and score_pct < float(max_exclusive):
            return dict(reject)
    continue_band = bands.get("continue_research_if_inexpensive")
    if isinstance(continue_band, Mapping):
        min_inclusive = float(continue_band.get("min_inclusive", 50))
        max_inclusive = float(continue_band.get("max_inclusive", 64.99))
        if min_inclusive <= score_pct <= max_inclusive:
            return dict(continue_band)
    controlled = bands.get("controlled_experiment_after_gates")
    if isinstance(controlled, Mapping):
        min_inclusive = float(controlled.get("min_inclusive", 65))
        max_inclusive = float(controlled.get("max_inclusive", 79.99))
        if min_inclusive <= score_pct <= max_inclusive:
            return dict(controlled)
    priority = bands.get("priority_experiment")
    if isinstance(priority, Mapping):
        min_inclusive = float(priority.get("min_inclusive", 80))
        if score_pct >= min_inclusive:
            return dict(priority)
    raise DecideError(f"score {score} did not match any interpretation band")


def select_decision_action(
    rerank: RerankResult,
    *,
    constraints: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any], RankedOpportunity]:
    """Deterministic routing action from re-ranked opportunities."""
    if not rerank.ranked:
        raise DecideError("rerank result is empty")
    active_constraints = constraints or load_constraints()
    top = rerank.ranked[0]
    niche_id = str(top.opportunity["niche_id"])
    any_pass = any(item.constraint_fit.passes for item in rerank.ranked)

    if not any_pass:
        return (
            "expand",
            {"niche_id": niche_id, "max_queries": 10},
            top,
        )

    if top.opportunity.get("coverage_gaps"):
        return (
            "expand",
            {"niche_id": niche_id, "max_queries": 10},
            top,
        )

    if not top.constraint_fit.passes:
        return "stop", {}, top

    band = interpret_score_band(float(top.opportunity["score"]), active_constraints)
    action_name = str(band.get("action", ""))
    if action_name in {
        "eligible_for_controlled_experiment_after_gates_pass",
        "priority_experiment_not_automatic_inventory_purchase",
    }:
        return (
            "synthesize",
            {"synthesis_id": f"syn_{top.opportunity['opportunity_id']}"},
            top,
        )
    if action_name == "continue_research_only_when_evidence_collection_is_inexpensive":
        return (
            "expand",
            {"niche_id": niche_id, "max_queries": 5},
            top,
        )
    return "stop", {}, top


def make_decision_id(
    *,
    action: str,
    args: Mapping[str, Any],
    derived_from: Sequence[str],
    config_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "action": action,
            "args": dict(args),
            "derived_from": list(derived_from),
            "config_hash": config_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"dec_{digest}"


def build_decision_v1(
    *,
    rerank: RerankResult,
    action: str,
    args: Mapping[str, Any],
    selected: RankedOpportunity,
    config_hash: str,
    created_at: str,
    cited_manifest_hash: str | None = None,
) -> dict[str, Any]:
    """Build deterministic decision.v1 shell without LLM rationale."""
    derived_from = [str(item.opportunity["opportunity_id"]) for item in rerank.ranked]
    manifest_hash = cited_manifest_hash or config_hash
    decision = {
        "decision_id": make_decision_id(
            action=action,
            args=args,
            derived_from=derived_from,
            config_hash=config_hash,
        ),
        "action": action,
        "args": dict(args),
        "rationale": {
            "text": "",
            "provenance": {
                "model_id": CASSETTE_MODEL_ID,
                "prompt_version": PROMPT_VERSION,
            },
        },
        "cited_manifest_hash": manifest_hash,
        "constraint_rank": selected.constraint_rank,
        "derived_from": derived_from,
        "provenance": {
            "code_version": CODE_VERSION,
            "schema_version": DECISION_SCHEMA_VERSION,
            "config_hash": config_hash,
            "created_at": created_at,
        },
        "schema_version": DECISION_SCHEMA_VERSION,
    }
    return decision


def build_replay_request(
    decision: Mapping[str, Any],
    *,
    model_id: str = CASSETTE_MODEL_ID,
) -> dict[str, Any]:
    return {
        "role": DECIDE_ROLE,
        "model_id": model_id,
        "prompt_version": PROMPT_VERSION,
        "decision_id": decision["decision_id"],
    }


def _validate_rationale_raw(raw: dict[str, Any]) -> tuple[str, ...]:
    violations: list[str] = []
    forbidden = sorted(LAW1_FORBIDDEN_KEYS.intersection(raw.keys()))
    if forbidden:
        violations.append(
            "LAW 1: decide rationale must not contain routing/scoring fields: "
            + ", ".join(forbidden)
        )
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        violations.append("decision.rationale.text must be a non-empty string")
    cited = raw.get("cited_record_ids")
    if cited is not None and not isinstance(cited, list):
        violations.append("decision.rationale.cited_record_ids must be an array when present")
    elif isinstance(cited, list):
        for record_id in cited:
            if not isinstance(record_id, str) or not record_id:
                violations.append("decision.rationale.cited_record_ids must contain non-empty strings")
    return tuple(violations)


def validate_rationale_output(raw: dict[str, Any]) -> None:
    violations = _validate_rationale_raw(raw)
    if violations:
        raise DecideError("; ".join(violations))


def assert_law1_decide_rationale_output(raw: dict[str, Any]) -> None:
    validate_rationale_output(raw)


def decide_split_verifier() -> VerifierFn:
    """Verifier decide-split: LLM rationale prose only; never action/rank/score."""

    def _verify(output: dict[str, Any], _packed_input: dict[str, Any]) -> VerifierResult:
        violations = list(_validate_rationale_raw(output))
        return VerifierResult(passed=not violations, violations=tuple(violations))

    return _verify


def rationale_verifier() -> VerifierFn:
    return decide_split_verifier()


def finalize_rationale(
    raw: dict[str, Any],
    *,
    model_id: str,
) -> dict[str, Any]:
    validate_rationale_output(raw)
    return {
        "text": str(raw["text"]).strip(),
        "cited_record_ids": list(raw.get("cited_record_ids") or []),
        "provenance": {
            "model_id": model_id,
            "prompt_version": PROMPT_VERSION,
        },
    }


def attach_rationale(
    decision: Mapping[str, Any],
    rationale: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach LLM rationale without mutating deterministic decision fields."""
    updated = copy.deepcopy(decision)
    before_action = updated.get("action")
    before_args = updated.get("args")
    before_rank = updated.get("constraint_rank")
    updated["rationale"] = {
        "text": rationale["text"],
        "cited_record_ids": list(rationale.get("cited_record_ids") or []),
        "provenance": dict(rationale["provenance"]),
    }
    if (
        updated.get("action") != before_action
        or updated.get("args") != before_args
        or updated.get("constraint_rank") != before_rank
    ):
        raise DecideError("decide rationale must not mutate action, args, or constraint_rank")
    validate_decision_v1(updated)
    return updated


def _build_messages(
    *,
    rerank: RerankResult,
    action: str,
    args: Mapping[str, Any],
    selected: RankedOpportunity,
    prior_output: dict[str, Any] | None = None,
    violations: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    user_content = {
        "node_id": "decide_rationale",
        "input_schema": "opportunity.v1[]",
        "output_schema": OUTPUT_SCHEMA,
        "packed_input": {
            "ranked_opportunities": [
                {
                    "opportunity_id": item.opportunity["opportunity_id"],
                    "score": item.opportunity["score"],
                    "constraint_fit": item.constraint_fit.as_dict(),
                    "constraint_rank": item.constraint_rank,
                    "combined_rank_score": item.combined_rank_score,
                }
                for item in rerank.ranked
            ],
            "selected_action": action,
            "selected_args": dict(args),
            "selected_opportunity_id": selected.opportunity["opportunity_id"],
        },
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": DECIDE_PROMPT},
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
        raise DecideError("LLM output must be a JSON object")
    return payload


def decide_rationale(
    decision: Mapping[str, Any],
    rerank: RerankResult,
    *,
    action: str,
    args: Mapping[str, Any],
    selected: RankedOpportunity,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    model_id: str = CASSETTE_MODEL_ID,
    max_iterations: int = MAX_ITERATIONS,
) -> DecideExecutionResult:
    """Run LLM rationale loop; never mutates deterministic decision routing."""
    request = replay_request or build_replay_request(decision, model_id=model_id)
    llm_gateway = gateway or LLMGateway(mode=GatewayMode.REPLAY)
    verify = decide_split_verifier()

    last_violations: tuple[str, ...] = ()
    last_output: dict[str, Any] | None = None
    replayed = False

    for attempt in range(1, max_iterations + 1):
        messages = _build_messages(
            rerank=rerank,
            action=action,
            args=args,
            selected=selected,
            prior_output=last_output if attempt > 1 else None,
            violations=last_violations if attempt > 1 else (),
        )
        completion = llm_gateway.generate(
            DECIDE_ROLE,
            messages,
            cassette_kind=CASSETTE_KIND,
            replay_request=request,
        )
        replayed = completion.replayed
        candidate = _extract_llm_output(completion.parsed, completion.text)
        result = verify(candidate, {})
        if result.passed:
            return DecideExecutionResult(
                verdict="pass",
                attempts=attempt,
                output=candidate,
                violations=(),
                replayed=replayed,
            )
        last_output = candidate
        last_violations = result.violations

    return DecideExecutionResult(
        verdict="ceiling",
        attempts=max_iterations,
        output=last_output,
        violations=last_violations,
        replayed=replayed,
    )


def write_decision_lineage_edges(
    conn: psycopg.Connection,
    decision: Mapping[str, Any],
) -> int:
    """Write LAW 2 lineage edges from decision.v1 to each derived opportunity."""
    from db.repositories.constraints import insert_lineage_edge_idempotent

    decision_id = decision["decision_id"]
    written = 0
    for opportunity_id in decision.get("derived_from", []):
        if insert_lineage_edge_idempotent(
            conn,
            child_kind="decision",
            child_id=str(decision_id),
            parent_kind="opportunity",
            parent_id=str(opportunity_id),
            relation="decided_from",
            version_tag=CODE_VERSION,
        ):
            written += 1
    return written


def run_decide_task(
    opportunities: Sequence[Mapping[str, Any]],
    *,
    candidate_profiles: Mapping[str, Mapping[str, Any]] | None = None,
    constraints: Mapping[str, Any] | None = None,
    config_hash: str | None = None,
    created_at: str,
    cited_manifest_hash: str | None = None,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    conn: psycopg.Connection | None = None,
) -> DecideSuccess:
    """Deterministic re-rank + LLM rationale; ranks never come from the LLM."""
    active_constraints = constraints or load_constraints()
    manifest_hash = config_hash or constraints_config_hash(active_constraints)
    rerank = rerank_opportunities(
        opportunities,
        candidate_profiles=candidate_profiles,
        constraints=active_constraints,
    )
    action, args, selected = select_decision_action(rerank, constraints=active_constraints)
    decision = build_decision_v1(
        rerank=rerank,
        action=action,
        args=args,
        selected=selected,
        config_hash=manifest_hash,
        created_at=created_at,
        cited_manifest_hash=cited_manifest_hash,
    )

    llm_gateway = gateway or LLMGateway(mode=GatewayMode.REPLAY)
    role_model_id = llm_gateway.resolve_role(DECIDE_ROLE).model_id
    execution = decide_rationale(
        decision,
        rerank,
        action=action,
        args=args,
        selected=selected,
        gateway=llm_gateway,
        replay_request=replay_request,
        model_id=role_model_id if llm_gateway.mode is GatewayMode.LIVE else CASSETTE_MODEL_ID,
    )
    if execution.verdict != "pass" or execution.output is None:
        raise DecideError(
            "decide rationale failed: "
            + ("; ".join(execution.violations) if execution.violations else execution.verdict)
        )

    rationale = finalize_rationale(
        execution.output,
        model_id=role_model_id,
    )
    finalized = attach_rationale(decision, rationale)
    edges_written = 0
    if conn is not None:
        edges_written = write_decision_lineage_edges(conn, finalized)
    return DecideSuccess(
        decision=finalized,
        rerank=rerank,
        attempts=execution.attempts,
        replayed=bool(execution.replayed),
        lineage_edges_written=edges_written,
    )


def decide_camping_fixture(
    *,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    created_at: str = "2026-07-21T12:00:00Z",
) -> DecideSuccess:
    """Score camping-fixture and produce a cassette-backed decision."""
    from signal_engine.score import score_camping_fixture

    opportunity = score_camping_fixture()
    profiles = {
        "nc-001": {
            "margin_potential": 4,
            "shipping_fit": 5,
            "community_reachability": 4,
        }
    }
    return run_decide_task(
        [opportunity],
        candidate_profiles=profiles,
        config_hash=str(opportunity["provenance"]["config_hash"]),
        created_at=created_at,
        gateway=gateway,
        replay_request=replay_request,
    )


__all__ = [
    "CASSETTE_KIND",
    "CASSETTE_MODEL_ID",
    "CODE_VERSION",
    "CONSTRAINT_AXES",
    "DECIDE_PROMPT",
    "DECIDE_ROLE",
    "DECISION_SCHEMA_VERSION",
    "DecideError",
    "DecideExecutionResult",
    "DecideSuccess",
    "ConstraintFit",
    "PROMPT_VERSION",
    "RankedOpportunity",
    "RerankResult",
    "assert_law1_decide_rationale_output",
    "attach_rationale",
    "build_decision_v1",
    "build_replay_request",
    "compute_constraint_fit",
    "constraints_config_hash",
    "decide_camping_fixture",
    "decide_rationale",
    "decide_split_verifier",
    "finalize_rationale",
    "interpret_score_band",
    "load_constraints",
    "make_decision_id",
    "rationale_verifier",
    "rerank_opportunities",
    "run_decide_task",
    "select_decision_action",
    "validate_decision_v1",
    "validate_rationale_output",
    "write_decision_lineage_edges",
]
