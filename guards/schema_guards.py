"""Schema and compile-time guards (doc 09 §1 guards 3, 5, 6, 8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from guards.exceptions import GuardViolation

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


def validate_instance(schema_name: str, instance: dict[str, Any]) -> None:
    schema = load_schema(schema_name)
    Draft202012Validator(schema).validate(instance)


def reject_invalid_instance(schema_name: str, instance: dict[str, Any]) -> None:
    """Raise GuardViolation when instance fails schema validation."""
    try:
        validate_instance(schema_name, instance)
    except jsonschema.ValidationError as exc:
        raise GuardViolation(str(exc.message)) from exc


def guard5_reject_llm_score_provenance(opportunity: dict[str, Any]) -> None:
    """LAW 1 write guard: score provenance must not contain model_id."""
    provenance = opportunity.get("provenance") or {}
    if "model_id" in provenance:
        raise GuardViolation(
            "LAW 1: opportunity.score provenance must not contain model_id"
        )
    reject_invalid_instance("opportunity.v1.schema.json", opportunity)


def guard6_reject_empty_lineage(signal: dict[str, Any]) -> None:
    """LAW 2: derived artifacts require non-empty derived_from."""
    derived_from = signal.get("derived_from")
    if not derived_from:
        raise GuardViolation("LAW 2: signal.derived_from must be non-empty")
    reject_invalid_instance("signal.v1.schema.json", signal)


def guard8_validate_workflow(workflow: dict[str, Any]) -> None:
    """Workflow compile-time schema: llm nodes need verifier; back-edges need max_trips."""
    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    node_ids = {node.get("id") for node in nodes}

    for node in nodes:
        if node.get("kind") != "llm":
            continue
        if not node.get("verifier"):
            raise GuardViolation(
                f"workflow node {node.get('id')!r}: kind=llm requires verifier"
            )

    for edge in edges:
        if edge.get("edge_type") != "back":
            continue
        from_node = edge.get("from")
        to_node = edge.get("to")
        if from_node in node_ids and to_node in node_ids and edge.get("max_trips") is None:
            raise GuardViolation(
                f"workflow back-edge {from_node}->{to_node} requires max_trips"
            )


def guard3_migration_declares_uniques(sql: str) -> None:
    """Guard 3 static half: migration must declare idempotency unique constraints."""
    required = (
        "CREATE UNIQUE INDEX idx_tasks_idempotency",
        "lineage_edges_unique_edge",
    )
    missing = [token for token in required if token not in sql]
    if missing:
        raise GuardViolation(
            "Guard 3 migration missing unique constraints: " + ", ".join(missing)
        )
