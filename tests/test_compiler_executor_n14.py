"""N14 compiler+executor — YAML→rows, Mermaid, execute via N12+N13."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import psycopg

from db.repositories.connection import connect
from db.repositories.migrate import apply_migrations
from fixtures.load import load_fixtures
from graph.compiler import (
    END_NODE,
    START_NODE,
    WorkflowCompileError,
    compile_workflow_yaml,
    persist_compiled_workflow,
    render_mermaid,
)
from graph.executor import (
    WorkflowExecutorError,
    build_node_definition,
    execute_compiled_node,
)
from graph.verifiers.catalog import CATALOG_VERIFIER_NAMES
from guards.exceptions import GuardViolation
from guards.schema_guards import guard8_validate_workflow
from harness.gateway import GatewayMode, LLMGateway
from harness.litellm_adapter import CassetteNotFoundError
from harness.node_executor import NodeKind
from lineage.edges import edges_for_child

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"
ENV_FILE = REPO_ROOT / ".env"

CONFIG_HASH = "sha256:" + ("a" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"

BUDGET = {
    "max_queries": 10,
    "max_fetched_urls": 100,
    "per_domain_urls": 50,
    "browser_pages": 5,
    "media_items": 10,
    "max_bytes": 1048576,
    "deadline_minutes": 30,
    "max_attempts": 3,
    "llm_budget": {"max_calls": 10, "max_tokens": 10000, "max_usd": 0},
    "schema_version": "budget.v1",
}

SAMPLE_WORKFLOW_YAML = """
workflow:
  id: wf_n14_enrich
  name: n14_enrich_slice
  version: "2026.07.21"
nodes:
  - id: enrich_page
    kind: llm
    role: enrich.primary
    input_schema: page.v1
    output_schema: evidence.v1
    prompt: Extract evidence from the page artifact.
    cassette_kind: enrich
    loop:
      max_iterations: 2
    verifier: schema_validate
edges:
  - from: __start__
    to: enrich_page
    edge_type: sequence
  - from: enrich_page
    to: __end__
    edge_type: sequence
"""


def _load_dotenv() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _postgres_available() -> bool:
    _load_dotenv()
    if not os.environ.get("POSTGRES_PASSWORD"):
        return False
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
        return True
    except (psycopg.Error, RuntimeError):
        return False


class CompileWorkflowTests(unittest.TestCase):
    def test_compile_produces_postgres_row_models(self) -> None:
        compiled = compile_workflow_yaml(SAMPLE_WORKFLOW_YAML)
        self.assertEqual(compiled.definition.workflow_id, "wf_n14_enrich")
        self.assertEqual(compiled.definition.name, "n14_enrich_slice")
        self.assertTrue(compiled.definition.graph_yaml_hash.startswith("sha256:"))
        self.assertEqual(len(compiled.nodes), 3)
        node = next(item for item in compiled.nodes if item.node_id == "enrich_page")
        self.assertEqual(node.node_id, "enrich_page")
        self.assertEqual(node.kind, "llm")
        self.assertEqual(node.role, "enrich.primary")
        self.assertEqual(node.input_schemas, ("page.v1",))
        self.assertEqual(node.output_schemas, ("evidence.v1",))
        self.assertEqual(node.verifier, "schema_validate")
        self.assertEqual(node.loop_budget, 2)
        self.assertEqual(len(compiled.edges), 2)
        self.assertEqual(compiled.edges[0].from_node, START_NODE)
        self.assertEqual(compiled.edges[0].to_node, "enrich_page")

    def test_compile_rejects_llm_node_without_verifier(self) -> None:
        bad_yaml = """
workflow:
  id: wf_bad
  name: bad
  version: "1"
nodes:
  - id: classify
    kind: llm
    role: classifier
    input_schema: page.v1
    output_schema: evidence.v1
    loop:
      max_iterations: 1
edges: []
"""
        with self.assertRaises(WorkflowCompileError) as ctx:
            compile_workflow_yaml(bad_yaml)
        self.assertIn("verifier", str(ctx.exception))

    def test_compile_rejects_unknown_verifier(self) -> None:
        bad_yaml = """
workflow:
  id: wf_bad
  name: bad
  version: "1"
nodes:
  - id: enrich_page
    kind: llm
    role: enrich.primary
    input_schema: page.v1
    output_schema: evidence.v1
    verifier: not_in_catalog
    loop:
      max_iterations: 1
edges: []
"""
        with self.assertRaises(WorkflowCompileError) as ctx:
            compile_workflow_yaml(bad_yaml)
        self.assertIn("unknown verifier", str(ctx.exception))

    def test_compile_rejects_back_edge_without_max_trips(self) -> None:
        payload = {
            "workflow": {"id": "wf_back", "name": "back", "version": "1"},
            "nodes": [
                {
                    "id": "loop_node",
                    "kind": "llm",
                    "role": "reason.primary",
                    "input_schema": "page.v1",
                    "output_schema": "evidence.v1",
                    "verifier": "schema_validate",
                    "loop": {"max_iterations": 1},
                }
            ],
            "edges": [
                {"from": "loop_node", "to": "loop_node", "edge_type": "back"},
            ],
        }
        with self.assertRaises(WorkflowCompileError):
            compile_workflow_yaml(json.dumps(payload))

    def test_guard8_still_applies_to_compile(self) -> None:
        with self.assertRaises(GuardViolation):
            guard8_validate_workflow(
                {
                    "nodes": [{"id": "x", "kind": "llm", "role": "r"}],
                    "edges": [],
                }
            )


class RenderMermaidTests(unittest.TestCase):
    def test_render_mermaid_includes_nodes_and_edge_labels(self) -> None:
        compiled = compile_workflow_yaml(SAMPLE_WORKFLOW_YAML)
        diagram = render_mermaid(compiled)
        self.assertIn("flowchart TD", diagram)
        self.assertIn('enrich_page["enrich_page\\nllm"]', diagram)
        self.assertIn("start([__start__])", diagram)
        self.assertIn("end([__end__])", diagram)
        self.assertIn("-->|sequence|", diagram)


class BuildNodeDefinitionTests(unittest.TestCase):
    def test_builds_n12_node_with_catalog_verifier(self) -> None:
        compiled = compile_workflow_yaml(SAMPLE_WORKFLOW_YAML)
        node = build_node_definition(compiled, "enrich_page")
        self.assertEqual(node.node_id, "enrich_page")
        self.assertEqual(node.kind, NodeKind.LLM)
        self.assertEqual(node.role, "enrich.primary")
        self.assertEqual(node.input_schema, "page.v1")
        self.assertEqual(node.output_schema, "evidence.v1")
        self.assertEqual(node.max_iterations, 2)
        self.assertIsNotNone(node.verifier)

    def test_unknown_node_raises(self) -> None:
        compiled = compile_workflow_yaml(SAMPLE_WORKFLOW_YAML)
        with self.assertRaises(WorkflowExecutorError):
            build_node_definition(compiled, "missing")


class ExecuteCompiledNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiled = compile_workflow_yaml(SAMPLE_WORKFLOW_YAML)
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        self.page = dict(corpus.page_goldens["review_page.page.v1.json"])
        cassette = corpus.cassettes["enrich"][0]
        self.replay_request = dict(cassette["request"])
        self.expected_output = dict(cassette["response"]["parsed"])

    def test_executes_llm_node_with_catalog_schema_validate(self) -> None:
        execution = execute_compiled_node(
            self.compiled,
            "enrich_page",
            self.page,
            gateway=self.gateway,
            replay_request=self.replay_request,
        )
        self.assertEqual(execution.workflow_id, "wf_n14_enrich")
        self.assertEqual(execution.result.verdict, "pass")
        self.assertTrue(execution.result.replayed)
        self.assertEqual(
            execution.result.output["record_id"],
            self.expected_output["record_id"],
        )

    def test_executes_deterministic_node(self) -> None:
        yaml_doc = """
workflow:
  id: wf_n14_det
  name: det
  version: "1"
nodes:
  - id: passthrough
    kind: deterministic
    input_schema: page.v1
    output_schema: evidence.v1
    verifier: schema_validate
    loop:
      max_iterations: 1
edges: []
"""
        compiled = compile_workflow_yaml(yaml_doc)
        expected = dict(self.expected_output)

        def passthrough(_page: dict) -> dict:
            return dict(expected)

        execution = execute_compiled_node(
            compiled,
            "passthrough",
            self.page,
            deterministic_fn=passthrough,
        )
        self.assertEqual(execution.result.verdict, "pass")
        self.assertEqual(execution.result.output["record_id"], expected["record_id"])


@unittest.skipUnless(_postgres_available(), "Postgres unavailable (need .env POSTGRES_*)")
class PersistCompiledWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = connect()
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.execute("SAVEPOINT n14_test_case")

    def tearDown(self) -> None:
        self.conn.execute("ROLLBACK TO SAVEPOINT n14_test_case")

    def test_persist_writes_workflow_rows(self) -> None:
        compiled = compile_workflow_yaml(SAMPLE_WORKFLOW_YAML)
        persist_compiled_workflow(self.conn, compiled)

        row = self.conn.execute(
            """
            SELECT workflow_id, name, version, graph_yaml_hash
            FROM workflow_defs
            WHERE workflow_id = %s
            """,
            (compiled.definition.workflow_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "n14_enrich_slice")
        self.assertEqual(row[3], compiled.definition.graph_yaml_hash)

        nodes = self.conn.execute(
            """
            SELECT node_id, kind, verifier, loop_budget, input_schemas, output_schemas
            FROM workflow_nodes
            WHERE workflow_id = %s
            ORDER BY node_id
            """,
            (compiled.definition.workflow_id,),
        ).fetchall()
        self.assertEqual(len(nodes), 3)
        enrich = next(row for row in nodes if row[0] == "enrich_page")
        self.assertEqual(enrich[0], "enrich_page")
        self.assertEqual(enrich[2], "schema_validate")
        input_schemas = enrich[4] if isinstance(enrich[4], list) else json.loads(enrich[4])
        self.assertEqual(input_schemas, ["page.v1"])

        edges = self.conn.execute(
            """
            SELECT from_node, to_node, edge_type
            FROM workflow_edges
            WHERE workflow_id = %s
            ORDER BY from_node, to_node
            """,
            (compiled.definition.workflow_id,),
        ).fetchall()
        self.assertEqual(len(edges), 2)


class IntegrationCheckCompilerExecutor(unittest.TestCase):
    """N14 integration_check: compile YAML→rows; render Mermaid; execute a node."""

    def test_integration_check_compiler_executor(self) -> None:
        compiled = compile_workflow_yaml(SAMPLE_WORKFLOW_YAML)
        self.assertEqual(compiled.nodes[0].verifier, "schema_validate")
        self.assertIn("schema_validate", CATALOG_VERIFIER_NAMES)

        diagram = render_mermaid(compiled)
        self.assertIn("enrich_page", diagram)
        self.assertIn("sequence", diagram)

        corpus = load_fixtures()
        page = dict(corpus.page_goldens["review_page.page.v1.json"])
        cassette = corpus.cassettes["enrich"][0]
        request = dict(cassette["request"])
        gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)

        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            execution = execute_compiled_node(
                compiled,
                "enrich_page",
                page,
                gateway=gateway,
                replay_request=request,
            )
            self.assertEqual(execution.result.verdict, "pass")
            self.assertTrue(execution.result.replayed)

            with self.assertRaises(CassetteNotFoundError):
                execute_compiled_node(
                    compiled,
                    "enrich_page",
                    page,
                    gateway=gateway,
                    replay_request={
                        "model_id": "qwen3-4b-q4",
                        "prompt_version": "missing",
                        "page_id": "pg_missing",
                    },
                )

        if _postgres_available():
            conn = connect()
            try:
                apply_migrations(conn)
                conn.execute("SAVEPOINT n14_integration")
                persist_compiled_workflow(conn, compiled)

                job_id = "job_n14_integration"
                run_id = "run_n14_integration"
                provenance = {
                    "schema_version": "job.v1",
                    "config_hash": CONFIG_HASH,
                    "created_at": CREATED_AT,
                }
                conn.execute(
                    """
                    INSERT INTO research_jobs (
                        job_id, job_kind, status, config_hash, budget, provenance
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (job_id) DO NOTHING
                    """,
                    (
                        job_id,
                        "dossier",
                        "CREATED",
                        CONFIG_HASH,
                        json.dumps(BUDGET),
                        json.dumps(provenance),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO workflow_runs (run_id, workflow_id, job_id, status)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (run_id, compiled.definition.workflow_id, job_id, "RUNNING"),
                )

                with patch.object(
                    httpx.Client,
                    "post",
                    side_effect=AssertionError("live LLM call attempted"),
                ):
                    persisted = execute_compiled_node(
                        compiled,
                        "enrich_page",
                        page,
                        conn=conn,
                        run_id=run_id,
                        gateway=gateway,
                        replay_request=request,
                    )
                self.assertGreaterEqual(persisted.lineage_edges_written, 1)
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT verdict
                        FROM node_executions
                        WHERE run_id = %s AND node_id = %s
                        """,
                        (run_id, "enrich_page"),
                    ).fetchone()[0],
                    "pass",
                )
                child_id = persisted.result.output["record_id"]
                edges = edges_for_child(
                    conn,
                    child_kind="evidence",
                    child_id=child_id,
                )
                self.assertTrue(edges)
            finally:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT n14_integration")
                except psycopg.Error:
                    pass
                conn.close()


if __name__ == "__main__":
    unittest.main()
