"""N31 graph/defs — research.yaml + dossier.yaml compile & execute."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from fixtures.load import load_fixtures
from graph.compiler import (
    END_NODE,
    START_NODE,
    compile_workflow_file,
    render_mermaid,
)
from graph.executor import execute_compiled_node
from graph.verifiers.catalog import CATALOG_VERIFIER_NAMES
from harness.gateway import GatewayMode, LLMGateway
from signal_engine.score import score_camping_fixture, validate_opportunity_v1

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_YAML = REPO_ROOT / "graph" / "defs" / "research.yaml"
DOSSIER_YAML = REPO_ROOT / "graph" / "defs" / "dossier.yaml"
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"


class CompileGraphDefsTests(unittest.TestCase):
    def test_research_yaml_compiles_doc_07_graph(self) -> None:
        compiled = compile_workflow_file(RESEARCH_YAML)
        self.assertEqual(compiled.definition.workflow_id, "wf_research")
        self.assertEqual(compiled.definition.name, "trail_signal_research")

        runtime_ids = set(compiled.runtime_nodes)
        self.assertEqual(
            runtime_ids,
            {
                "planner",
                "discover",
                "fetch_parse",
                "enrich_page",
                "index_rollup",
                "gap_analyst",
                "synthesizer",
                "reviewer",
            },
        )

        enrich = compiled.runtime_nodes["enrich_page"]
        self.assertEqual(enrich.kind, "llm")
        self.assertEqual(enrich.role, "enrich.primary")
        self.assertEqual(enrich.input_schema, "page.v1")
        self.assertEqual(enrich.output_schema, "evidence.v1")
        self.assertEqual(enrich.verifier, "schema_validate")
        self.assertEqual(enrich.cassette_kind, "enrich")

        planner = compiled.runtime_nodes["planner"]
        self.assertEqual(planner.verifier, "plan_validates")
        self.assertEqual(compiled.runtime_nodes["gap_analyst"].verifier, "decision_valid")
        self.assertEqual(compiled.runtime_nodes["reviewer"].verifier, "claim_grounding")

        edge_types = {edge.edge_type for edge in compiled.edges}
        self.assertIn("fan-out", edge_types)
        self.assertIn("fan-in", edge_types)
        self.assertIn("back", edge_types)

        back_edges = [edge for edge in compiled.edges if edge.edge_type == "back"]
        self.assertTrue(all(edge.max_trips is not None for edge in back_edges))

        node_ids = {node.node_id for node in compiled.nodes}
        self.assertIn(START_NODE, node_ids)
        self.assertIn(END_NODE, node_ids)

    def test_dossier_yaml_compiles_pipeline(self) -> None:
        compiled = compile_workflow_file(DOSSIER_YAML)
        self.assertEqual(compiled.definition.workflow_id, "wf_dossier")
        self.assertEqual(compiled.definition.name, "trail_signal_dossier")

        ordered = [
            "signal_normalize",
            "coverage_gate",
            "score_opportunity",
            "explain_opportunity",
            "validate_dossier",
            "decide",
        ]
        self.assertEqual(list(compiled.runtime_nodes.keys()), ordered)

        score = compiled.runtime_nodes["score_opportunity"]
        self.assertEqual(score.kind, "deterministic")
        self.assertEqual(score.output_schema, "opportunity.v1")
        self.assertEqual(score.verifier, "schema_validate")

        decide = compiled.runtime_nodes["decide"]
        self.assertEqual(decide.kind, "llm")
        self.assertEqual(decide.output_schema, "decision.v1")
        self.assertEqual(decide.verifier, "decision_valid")

        sequence_edges = [
            (edge.from_node, edge.to_node)
            for edge in compiled.edges
            if edge.edge_type == "sequence"
        ]
        self.assertEqual(
            sequence_edges,
            [
                (START_NODE, "signal_normalize"),
                ("signal_normalize", "coverage_gate"),
                ("coverage_gate", "score_opportunity"),
                ("score_opportunity", "explain_opportunity"),
                ("explain_opportunity", "validate_dossier"),
                ("validate_dossier", "decide"),
                ("decide", END_NODE),
            ],
        )

    def test_all_referenced_verifiers_are_in_catalog(self) -> None:
        for path in (RESEARCH_YAML, DOSSIER_YAML):
            compiled = compile_workflow_file(path)
            for runtime in compiled.runtime_nodes.values():
                if runtime.kind == "llm":
                    self.assertIn(runtime.verifier, CATALOG_VERIFIER_NAMES)


class RenderGraphDefsTests(unittest.TestCase):
    def test_render_mermaid_for_both_workflows(self) -> None:
        for path in (RESEARCH_YAML, DOSSIER_YAML):
            compiled = compile_workflow_file(path)
            diagram = render_mermaid(compiled)
            self.assertIn("flowchart TD", diagram)
            self.assertIn("sequence", diagram)
            for runtime in compiled.runtime_nodes.values():
                self.assertIn(runtime.node_id, diagram)


class ExecuteGraphDefsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = LLMGateway(models_path=MODELS_PATH, mode=GatewayMode.REPLAY)
        corpus = load_fixtures()
        self.page = dict(corpus.page_goldens["review_page.page.v1.json"])
        cassette = corpus.cassettes["enrich"][0]
        self.enrich_request = dict(cassette["request"])
        self.expected_evidence = dict(cassette["response"]["parsed"])

    def test_research_enrich_page_executes_with_cassette(self) -> None:
        compiled = compile_workflow_file(RESEARCH_YAML)
        with patch.object(httpx.Client, "post", side_effect=AssertionError("live LLM call attempted")):
            execution = execute_compiled_node(
                compiled,
                "enrich_page",
                self.page,
                gateway=self.gateway,
                replay_request=self.enrich_request,
            )
        self.assertEqual(execution.workflow_id, "wf_research")
        self.assertEqual(execution.node_id, "enrich_page")
        self.assertEqual(execution.result.verdict, "pass")
        self.assertTrue(execution.result.replayed)
        self.assertEqual(
            execution.result.output["record_id"],
            self.expected_evidence["record_id"],
        )

    def test_dossier_score_opportunity_executes_deterministically(self) -> None:
        compiled = compile_workflow_file(DOSSIER_YAML)
        expected = score_camping_fixture()
        validate_opportunity_v1(expected)
        corpus = load_fixtures()
        signal = dict(corpus.camping_signals["signals"][0])

        def score_fn(_packed: dict) -> dict:
            return dict(expected)

        execution = execute_compiled_node(
            compiled,
            "score_opportunity",
            signal,
            deterministic_fn=score_fn,
        )
        self.assertEqual(execution.workflow_id, "wf_dossier")
        self.assertEqual(execution.node_id, "score_opportunity")
        self.assertEqual(execution.result.verdict, "pass")
        self.assertAlmostEqual(execution.result.output["score"], expected["score"])


class IntegrationCheckGraphDefs(unittest.TestCase):
    """N31 integration_check: research.yaml + dossier.yaml compile & execute."""

    def test_integration_check_graph_defs(self) -> None:
        research = compile_workflow_file(RESEARCH_YAML)
        dossier = compile_workflow_file(DOSSIER_YAML)

        self.assertIn("enrich_page", research.runtime_nodes)
        self.assertIn("score_opportunity", dossier.runtime_nodes)
        self.assertIn("flowchart TD", render_mermaid(research))
        self.assertIn("flowchart TD", render_mermaid(dossier))

        suite = unittest.TestSuite()
        suite.addTest(ExecuteGraphDefsTests("test_research_enrich_page_executes_with_cassette"))
        suite.addTest(ExecuteGraphDefsTests("test_dossier_score_opportunity_executes_deterministically"))
        runner = unittest.TextTestRunner()
        result = runner.run(suite)
        self.assertTrue(result.wasSuccessful())


if __name__ == "__main__":
    unittest.main()
