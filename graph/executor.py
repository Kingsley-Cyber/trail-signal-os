"""Execute compiled workflow nodes via N12 harness + N13 verifiers (N14)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from graph.compiler import CompiledNode, CompiledWorkflow
from graph.verifiers.catalog import get_verifier_factory
from harness.gateway import LLMGateway
from harness.node_executor import (
    DeterministicFn,
    NodeDefinition,
    NodeExecutionResult,
    NodeExecutorError,
    NodeKind,
    VerifierFn,
    execute_node,
)
from lineage.edges import write_lineage_edge

HUMAN_GATE_KIND = "human_gate"


class WorkflowExecutorError(Exception):
    """Workflow node execution failed."""


@dataclass(frozen=True)
class WorkflowNodeExecution:
    workflow_id: str
    node_id: str
    result: NodeExecutionResult
    lineage_edges_written: int


def resolve_verifier(
    runtime_node: CompiledNode,
    *,
    gateway: LLMGateway | None = None,
) -> VerifierFn:
    """Resolve a catalog verifier factory into a runtime verifier function."""
    factory = get_verifier_factory(runtime_node.verifier)
    if runtime_node.verifier == "schema_validate":
        return factory(runtime_node.output_schema)
    if runtime_node.verifier == "sample_judge":
        return factory(gateway=gateway)
    return factory()


def build_node_definition(
    compiled: CompiledWorkflow,
    node_id: str,
    *,
    gateway: LLMGateway | None = None,
) -> NodeDefinition:
    """Build an N12 NodeDefinition from a compiled workflow node."""
    try:
        runtime = compiled.runtime_nodes[node_id]
    except KeyError as exc:
        raise WorkflowExecutorError(f"unknown workflow node {node_id!r}") from exc

    if runtime.kind == HUMAN_GATE_KIND:
        raise WorkflowExecutorError(
            f"node {node_id!r}: kind=human_gate is not executable in the harness"
        )

    kind = NodeKind.LLM if runtime.kind == "llm" else NodeKind.DETERMINISTIC
    verifier = resolve_verifier(runtime, gateway=gateway)
    return NodeDefinition(
        node_id=runtime.node_id,
        kind=kind,
        role=runtime.role,
        input_schema=runtime.input_schema,
        output_schema=runtime.output_schema,
        prompt=runtime.prompt,
        cassette_kind=runtime.cassette_kind,
        max_iterations=runtime.max_iterations,
        verifier=verifier,
    )


def _artifact_kind(schema_version: str | None) -> str:
    if isinstance(schema_version, str) and schema_version.endswith(".v1"):
        return schema_version.removesuffix(".v1")
    return "artifact"


def _write_output_lineage(
    conn: psycopg.Connection | None,
    *,
    output: dict[str, Any],
    packed_input: dict[str, Any],
) -> int:
    if conn is None:
        return 0

    child_id = output.get("record_id") or output.get("decision_id") or output.get("page_id")
    if not isinstance(child_id, str) or not child_id:
        return 0

    child_kind = _artifact_kind(output.get("schema_version"))
    derived_from = output.get("derived_from")
    parent_ids: list[str]
    if isinstance(derived_from, list) and derived_from:
        parent_ids = [item for item in derived_from if isinstance(item, str) and item]
    else:
        fallback = packed_input.get("page_id") or packed_input.get("record_id")
        parent_ids = [fallback] if isinstance(fallback, str) and fallback else []

    if not parent_ids:
        return 0

    parent_kind = "page"
    if child_kind == "decision":
        parent_kind = "opportunity"
    written = 0
    for parent_id in parent_ids:
        if write_lineage_edge(
            conn,
            child_kind=child_kind,
            child_id=child_id,
            parent_kind=parent_kind,
            parent_id=parent_id,
            relation="derived_from",
        ):
            written += 1
    return written


def record_node_execution(
    conn: psycopg.Connection,
    *,
    run_id: str,
    node_id: str,
    result: NodeExecutionResult,
    task_id: str | None = None,
) -> None:
    """Append a node_executions row for a workflow run."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO node_executions (
                run_id,
                node_id,
                task_id,
                attempt,
                verdict,
                decision_ref
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, node_id, attempt) DO UPDATE SET
                task_id = EXCLUDED.task_id,
                verdict = EXCLUDED.verdict,
                decision_ref = EXCLUDED.decision_ref
            """,
            (
                run_id,
                node_id,
                task_id,
                result.attempts,
                result.verdict,
                (result.output or {}).get("decision_id"),
            ),
        )


def execute_compiled_node(
    compiled: CompiledWorkflow,
    node_id: str,
    packed_input: dict[str, Any],
    *,
    conn: psycopg.Connection | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    gateway: LLMGateway | None = None,
    replay_request: dict[str, Any] | None = None,
    deterministic_fn: DeterministicFn | None = None,
) -> WorkflowNodeExecution:
    """Execute one compiled workflow node and optionally persist execution metadata."""
    node = build_node_definition(compiled, node_id, gateway=gateway)
    try:
        result = execute_node(
            node,
            packed_input,
            gateway=gateway,
            replay_request=replay_request,
            deterministic_fn=deterministic_fn,
        )
    except NodeExecutorError as exc:
        raise WorkflowExecutorError(str(exc)) from exc

    lineage_edges_written = 0
    if result.output is not None and result.verdict == "pass":
        lineage_edges_written = _write_output_lineage(
            conn,
            output=result.output,
            packed_input=packed_input,
        )
    if conn is not None and run_id is not None:
        record_node_execution(
            conn,
            run_id=run_id,
            node_id=node_id,
            result=result,
            task_id=task_id,
        )

    return WorkflowNodeExecution(
        workflow_id=compiled.definition.workflow_id,
        node_id=node_id,
        result=result,
        lineage_edges_written=lineage_edges_written,
    )


__all__ = [
    "WorkflowExecutorError",
    "WorkflowNodeExecution",
    "build_node_definition",
    "execute_compiled_node",
    "record_node_execution",
    "resolve_verifier",
]
