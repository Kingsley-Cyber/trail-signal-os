"""Compile workflow YAML to Postgres rows and Mermaid (N14)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml

from graph.verifiers.catalog import CATALOG_VERIFIER_NAMES
from guards.exceptions import GuardViolation
from guards.schema_guards import guard8_validate_workflow

START_NODE = "__start__"
END_NODE = "__end__"


class WorkflowCompileError(Exception):
    """Workflow YAML failed compile-time validation."""


@dataclass(frozen=True)
class WorkflowDefRow:
    workflow_id: str
    name: str
    version: str
    graph_yaml_hash: str


@dataclass(frozen=True)
class WorkflowNodeRow:
    workflow_id: str
    node_id: str
    kind: str
    role: str | None
    input_schemas: tuple[str, ...]
    output_schemas: tuple[str, ...]
    verifier: str | None
    loop_budget: int | None


@dataclass(frozen=True)
class WorkflowEdgeRow:
    workflow_id: str
    from_node: str
    to_node: str
    edge_type: str
    condition_expr: str | None
    max_trips: int | None


@dataclass(frozen=True)
class CompiledNode:
    node_id: str
    kind: str
    role: str | None
    input_schema: str
    output_schema: str
    verifier: str
    max_iterations: int
    prompt: str | None = None
    cassette_kind: str | None = None


@dataclass(frozen=True)
class CompiledWorkflow:
    definition: WorkflowDefRow
    nodes: tuple[WorkflowNodeRow, ...]
    edges: tuple[WorkflowEdgeRow, ...]
    runtime_nodes: dict[str, CompiledNode]
    source_yaml: str


def _yaml_hash(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowCompileError(f"{label} must be a mapping")
    return value


def _schema_ref(node: dict[str, Any], key: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkflowCompileError(f"node {node.get('id')!r} requires {key}")
    return value.strip()


def _loop_budget(node: dict[str, Any]) -> int:
    loop = node.get("loop") or {}
    if not isinstance(loop, dict):
        raise WorkflowCompileError(f"node {node.get('id')!r}: loop must be a mapping")
    max_iterations = loop.get("max_iterations", 1)
    if not isinstance(max_iterations, int) or max_iterations < 1:
        raise WorkflowCompileError(
            f"node {node.get('id')!r}: loop.max_iterations must be an integer >= 1"
        )
    return max_iterations


def _validate_verifier_name(node_id: str, verifier: str | None, *, kind: str) -> str | None:
    if kind != "llm" and verifier is None:
        return None
    if verifier is None:
        raise WorkflowCompileError(f"node {node_id!r}: verifier is required")
    if not isinstance(verifier, str) or not verifier.strip():
        raise WorkflowCompileError(f"node {node_id!r}: verifier must be a non-empty string")
    name = verifier.strip()
    if name not in CATALOG_VERIFIER_NAMES:
        raise WorkflowCompileError(
            f"node {node_id!r}: unknown verifier {name!r} (expected one of "
            f"{', '.join(CATALOG_VERIFIER_NAMES)})"
        )
    return name


def compile_workflow_yaml(source: str) -> CompiledWorkflow:
    """Parse workflow YAML, validate guard 8 rules, and produce Postgres row models."""
    try:
        payload = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise WorkflowCompileError(f"invalid YAML: {exc}") from exc

    document = _require_mapping(payload, label="workflow document")
    meta = _require_mapping(document.get("workflow") or {}, label="workflow")
    workflow_id = meta.get("id")
    name = meta.get("name")
    version = meta.get("version")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise WorkflowCompileError("workflow.id is required")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowCompileError("workflow.name is required")
    if not isinstance(version, str) or not version.strip():
        raise WorkflowCompileError("workflow.version is required")

    workflow_id = workflow_id.strip()
    name = name.strip()
    version = version.strip()

    raw_nodes = document.get("nodes") or []
    raw_edges = document.get("edges") or []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise WorkflowCompileError("workflow requires at least one node")
    if not isinstance(raw_edges, list):
        raise WorkflowCompileError("workflow edges must be a list")

    guard_payload = {"nodes": raw_nodes, "edges": raw_edges}
    try:
        guard8_validate_workflow(guard_payload)
    except GuardViolation as exc:
        raise WorkflowCompileError(str(exc)) from exc

    node_rows: list[WorkflowNodeRow] = []
    runtime_nodes: dict[str, CompiledNode] = {}
    seen_ids: set[str] = set()

    for raw_node in raw_nodes:
        node = _require_mapping(raw_node, label="node")
        node_id = node.get("id")
        kind = node.get("kind")
        if not isinstance(node_id, str) or not node_id.strip():
            raise WorkflowCompileError("each node requires a non-empty id")
        if not isinstance(kind, str) or not kind.strip():
            raise WorkflowCompileError(f"node {node_id!r} requires kind")
        node_id = node_id.strip()
        kind = kind.strip()
        if node_id in seen_ids:
            raise WorkflowCompileError(f"duplicate node id {node_id!r}")
        seen_ids.add(node_id)

        role = node.get("role")
        if role is not None and not isinstance(role, str):
            raise WorkflowCompileError(f"node {node_id!r}: role must be a string")
        if kind == "llm" and not role:
            raise WorkflowCompileError(f"node {node_id!r}: kind=llm requires role")

        input_schema = _schema_ref(node, "input_schema")
        output_schema = _schema_ref(node, "output_schema")
        verifier = _validate_verifier_name(node_id, node.get("verifier"), kind=kind)
        loop_budget = _loop_budget(node)

        prompt = node.get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            raise WorkflowCompileError(f"node {node_id!r}: prompt must be a string")
        cassette_kind = node.get("cassette_kind")
        if cassette_kind is not None and not isinstance(cassette_kind, str):
            raise WorkflowCompileError(f"node {node_id!r}: cassette_kind must be a string")

        node_rows.append(
            WorkflowNodeRow(
                workflow_id=workflow_id,
                node_id=node_id,
                kind=kind,
                role=role,
                input_schemas=(input_schema,),
                output_schemas=(output_schema,),
                verifier=verifier,
                loop_budget=loop_budget,
            )
        )
        runtime_nodes[node_id] = CompiledNode(
            node_id=node_id,
            kind=kind,
            role=role,
            input_schema=input_schema,
            output_schema=output_schema,
            verifier=verifier or "schema_validate",
            max_iterations=loop_budget,
            prompt=prompt,
            cassette_kind=cassette_kind,
        )

    edge_rows: list[WorkflowEdgeRow] = []
    for raw_edge in raw_edges:
        edge = _require_mapping(raw_edge, label="edge")
        from_node = edge.get("from")
        to_node = edge.get("to")
        edge_type = edge.get("edge_type")
        if not isinstance(from_node, str) or not from_node.strip():
            raise WorkflowCompileError("edge.from is required")
        if not isinstance(to_node, str) or not to_node.strip():
            raise WorkflowCompileError("edge.to is required")
        if not isinstance(edge_type, str) or not edge_type.strip():
            raise WorkflowCompileError("edge.edge_type is required")

        from_node = from_node.strip()
        to_node = to_node.strip()
        edge_type = edge_type.strip()

        if from_node not in seen_ids and from_node != START_NODE:
            raise WorkflowCompileError(f"edge.from references unknown node {from_node!r}")
        if to_node not in seen_ids and to_node != END_NODE:
            raise WorkflowCompileError(f"edge.to references unknown node {to_node!r}")

        condition_expr = edge.get("condition")
        if condition_expr is not None and not isinstance(condition_expr, str):
            raise WorkflowCompileError(
                f"edge {from_node}->{to_node}: condition must be a string when present"
            )
        max_trips = edge.get("max_trips")
        if max_trips is not None and (not isinstance(max_trips, int) or max_trips < 1):
            raise WorkflowCompileError(
                f"edge {from_node}->{to_node}: max_trips must be an integer >= 1"
            )

        edge_rows.append(
            WorkflowEdgeRow(
                workflow_id=workflow_id,
                from_node=from_node,
                to_node=to_node,
                edge_type=edge_type,
                condition_expr=condition_expr,
                max_trips=max_trips,
            )
        )

    pseudo_nodes: list[WorkflowNodeRow] = []
    referenced = {edge.from_node for edge in edge_rows} | {edge.to_node for edge in edge_rows}
    for pseudo_id, pseudo_kind in ((START_NODE, "entry"), (END_NODE, "exit")):
        if pseudo_id not in referenced:
            continue
        pseudo_nodes.append(
            WorkflowNodeRow(
                workflow_id=workflow_id,
                node_id=pseudo_id,
                kind=pseudo_kind,
                role=None,
                input_schemas=(),
                output_schemas=(),
                verifier=None,
                loop_budget=None,
            )
        )

    definition = WorkflowDefRow(
        workflow_id=workflow_id,
        name=name,
        version=version,
        graph_yaml_hash=_yaml_hash(source),
    )
    return CompiledWorkflow(
        definition=definition,
        nodes=tuple(node_rows + pseudo_nodes),
        edges=tuple(edge_rows),
        runtime_nodes=runtime_nodes,
        source_yaml=source,
    )


def compile_workflow_file(path: Path | str) -> CompiledWorkflow:
    source_path = Path(path)
    return compile_workflow_yaml(source_path.read_text(encoding="utf-8"))


def _mermaid_id(node_id: str) -> str:
    safe = node_id.replace("-", "_").replace(".", "_")
    if safe == START_NODE:
        return "start"
    if safe == END_NODE:
        return "end"
    return safe


def render_mermaid(compiled: CompiledWorkflow) -> str:
    """Render control-flow Mermaid from compiled workflow edges (doc 07 gut-check #3)."""
    lines = ["flowchart TD"]
    declared_nodes = {node.node_id for node in compiled.nodes}
    declared_nodes.update({edge.from_node for edge in compiled.edges})
    declared_nodes.update({edge.to_node for edge in compiled.edges})
    for node_id in sorted(declared_nodes):
        if node_id in {START_NODE, END_NODE}:
            lines.append(f"  {_mermaid_id(node_id)}([{node_id}])")
        else:
            runtime = compiled.runtime_nodes.get(node_id)
            label = node_id
            if runtime is not None:
                label = f"{node_id}\\n{runtime.kind}"
            lines.append(f'  {_mermaid_id(node_id)}["{label}"]')

    for edge in compiled.edges:
        label = edge.edge_type
        if edge.condition_expr:
            label = f"{label}: {edge.condition_expr}"
        if edge.max_trips is not None:
            label = f"{label} (max_trips={edge.max_trips})"
        lines.append(
            f"  {_mermaid_id(edge.from_node)} -->|{label}| {_mermaid_id(edge.to_node)}"
        )
    return "\n".join(lines) + "\n"


def persist_compiled_workflow(conn: psycopg.Connection, compiled: CompiledWorkflow) -> None:
    """Upsert workflow_defs/nodes/edges rows for a compiled workflow."""
    definition = compiled.definition
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_defs (workflow_id, name, version, graph_yaml_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workflow_id) DO UPDATE SET
                name = EXCLUDED.name,
                version = EXCLUDED.version,
                graph_yaml_hash = EXCLUDED.graph_yaml_hash
            """,
            (
                definition.workflow_id,
                definition.name,
                definition.version,
                definition.graph_yaml_hash,
            ),
        )
        cur.execute(
            "DELETE FROM workflow_nodes WHERE workflow_id = %s",
            (definition.workflow_id,),
        )
        cur.execute(
            "DELETE FROM workflow_edges WHERE workflow_id = %s",
            (definition.workflow_id,),
        )
        for node in compiled.nodes:
            cur.execute(
                """
                INSERT INTO workflow_nodes (
                    workflow_id,
                    node_id,
                    kind,
                    role,
                    input_schemas,
                    output_schemas,
                    verifier,
                    loop_budget
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                """,
                (
                    node.workflow_id,
                    node.node_id,
                    node.kind,
                    node.role,
                    json.dumps(list(node.input_schemas)),
                    json.dumps(list(node.output_schemas)),
                    node.verifier,
                    node.loop_budget,
                ),
            )
        for edge in compiled.edges:
            cur.execute(
                """
                INSERT INTO workflow_edges (
                    workflow_id,
                    from_node,
                    to_node,
                    edge_type,
                    condition_expr,
                    max_trips
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    edge.workflow_id,
                    edge.from_node,
                    edge.to_node,
                    edge.edge_type,
                    edge.condition_expr,
                    edge.max_trips,
                ),
            )


__all__ = [
    "CompiledNode",
    "CompiledWorkflow",
    "END_NODE",
    "START_NODE",
    "WorkflowCompileError",
    "WorkflowDefRow",
    "WorkflowEdgeRow",
    "WorkflowNodeRow",
    "compile_workflow_file",
    "compile_workflow_yaml",
    "persist_compiled_workflow",
    "render_mermaid",
]
