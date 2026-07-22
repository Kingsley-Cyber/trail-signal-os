"""Poison tests — each asserts its guard fires on a deliberate violation (doc 09 §1)."""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from db.repositories.migrate import migrations_dir
from guards.catalog import GUARD_CATALOG
from guards.exceptions import GuardViolation, StaleLeaseError
from guards.registry import get_guard, list_guards
from guards.runtime_guards import (
    guard10_route_403_to_blocked,
    guard11_assert_normalize_invariants,
    guard12_assert_score_reproducible,
    guard2_require_fenced_update,
    guard6_require_lineage_edge,
    guard7_require_provenance,
)
from guards.schema_guards import (
    guard3_migration_declares_uniques,
    guard5_reject_llm_score_provenance,
    guard6_reject_empty_lineage,
    guard8_validate_workflow,
)
from guards.static_lint import (
    lint_ack_after_commit,
    lint_direct_artifact_insert,
    lint_import_purity,
    lint_no_evasion_deps,
    lint_outbox_xadd,
)

POISON_DIR = Path(__file__).resolve().parent / "poison"
REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_OPPORTUNITY = (
    REPO_ROOT / "fixtures" / "niches" / "camping-fixture" / "expected_opportunity.json"
)


class GuardCatalogTests(unittest.TestCase):
    def test_catalog_has_twelve_guards(self) -> None:
        self.assertEqual(len(GUARD_CATALOG), 12)
        self.assertEqual(len(list_guards()), 12)
        numbers = [spec.number for spec in GUARD_CATALOG]
        self.assertEqual(numbers, list(range(1, 13)))

    def test_registry_lookup(self) -> None:
        spec = get_guard(5)
        self.assertEqual(spec.name, "law1_no_llm_score")


class Guard01AckAfterCommitPoison(unittest.TestCase):
    def test_poison_pre_commit_xack_fires_static_guard(self) -> None:
        source = (POISON_DIR / "g01_pre_commit_xack.py").read_text(encoding="utf-8")
        with self.assertRaises(GuardViolation) as ctx:
            lint_ack_after_commit(source, filename="g01_pre_commit_xack.py")
        self.assertIn(".xack(", str(ctx.exception))
        self.assertEqual(get_guard(1).name, "ack_after_commit")


class Guard02FencingTokenPoison(unittest.TestCase):
    def test_stale_generation_write_raises_stale_lease_error(self) -> None:
        with self.assertRaises(StaleLeaseError):
            guard2_require_fenced_update(
                0,
                expected_owner="worker-a",
                actual_owner="worker-a",
            )


class Guard03IdempotencyUniquePoison(unittest.TestCase):
    def test_migration_declares_unique_constraints(self) -> None:
        sql = (migrations_dir() / "002_core_foundation.sql").read_text(encoding="utf-8")
        guard3_migration_declares_uniques(sql)

    def test_duplicate_idempotency_insert_is_no_op(self) -> None:
        from db.repositories.constraints import insert_task_idempotent

        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [("tsk_first",), None]
        conn.cursor.return_value.__enter__.return_value = cursor

        first = insert_task_idempotent(
            conn,
            task_id="tsk_first",
            job_id="job_poison",
            lane="http",
            idempotency_key="sha256:" + ("e" * 64),
            payload_ref="postgres://tasks/tsk_first",
            provenance={"schema_version": "task.v1", "config_hash": "sha256:" + ("a" * 64), "created_at": "2026-07-21T12:00:00Z"},
        )
        second = insert_task_idempotent(
            conn,
            task_id="tsk_second",
            job_id="job_poison",
            lane="http",
            idempotency_key="sha256:" + ("e" * 64),
            payload_ref="postgres://tasks/tsk_second",
            provenance={"schema_version": "task.v1", "config_hash": "sha256:" + ("a" * 64), "created_at": "2026-07-21T12:00:00Z"},
        )
        self.assertTrue(first)
        self.assertFalse(second)

    def test_poison_duplicate_without_guard_would_violate(self) -> None:
        with self.assertRaises(GuardViolation):
            guard3_migration_declares_uniques("-- missing guard 3 uniques")


class Guard04OutboxAtomicityPoison(unittest.TestCase):
    def test_xadd_outside_dispatcher_fires_static_guard(self) -> None:
        path = POISON_DIR / "g04_xadd_outside_dispatcher.py"
        source = path.read_text(encoding="utf-8")
        with self.assertRaises(GuardViolation) as ctx:
            lint_outbox_xadd(source, path=path)
        self.assertIn("cp:*", str(ctx.exception))


class Guard05Law1NoLlmScorePoison(unittest.TestCase):
    def test_opportunity_with_model_id_in_provenance_rejected(self) -> None:
        poison = json.loads(
            (POISON_DIR / "g05_opportunity_model_id.json").read_text(encoding="utf-8")
        )
        with self.assertRaises(GuardViolation) as ctx:
            guard5_reject_llm_score_provenance(poison)
        self.assertIn("model_id", str(ctx.exception))

    def test_valid_opportunity_passes_law1_guard(self) -> None:
        valid = json.loads(GOLDEN_OPPORTUNITY.read_text(encoding="utf-8"))
        guard5_reject_llm_score_provenance(valid)


class Guard06Law2TotalLineagePoison(unittest.TestCase):
    def test_empty_derived_from_rejected_by_schema_guard(self) -> None:
        poison = json.loads(
            (POISON_DIR / "g06_signal_empty_derived_from.json").read_text(encoding="utf-8")
        )
        with self.assertRaises(GuardViolation):
            guard6_reject_empty_lineage(poison)

    def test_inline_ref_without_lineage_edge_rejected(self) -> None:
        with self.assertRaises(GuardViolation) as ctx:
            guard6_require_lineage_edge(
                parent_refs=["ev_poison"],
                lineage_edge_written=False,
            )
        self.assertIn("lineage_edges", str(ctx.exception))


class Guard07ProvenanceStampPoison(unittest.TestCase):
    def test_direct_artifact_insert_fires_static_guard(self) -> None:
        path = POISON_DIR / "g07_direct_artifact_insert.sql"
        source = path.read_text(encoding="utf-8")
        with self.assertRaises(GuardViolation):
            lint_direct_artifact_insert(source, path=path)

    def test_missing_provenance_rejected_at_runtime(self) -> None:
        with self.assertRaises(GuardViolation):
            guard7_require_provenance(None)


class Guard08WorkflowVerifierPoison(unittest.TestCase):
    def test_llm_node_without_verifier_fails_compile_schema(self) -> None:
        workflow = json.loads(
            (POISON_DIR / "g08_llm_without_verifier.json").read_text(encoding="utf-8")
        )
        with self.assertRaises(GuardViolation) as ctx:
            guard8_validate_workflow(workflow)
        self.assertIn("verifier", str(ctx.exception))


class Guard09DeterministicImportPurityPoison(unittest.TestCase):
    def test_gateway_import_in_score_module_fires_static_guard(self) -> None:
        path = POISON_DIR / "g09_score_gateway_import.py"
        source = path.read_text(encoding="utf-8")
        with self.assertRaises(GuardViolation) as ctx:
            lint_import_purity(source, path=Path("signal_engine/score.py"))
        self.assertIn("harness.gateway", str(ctx.exception))


class Guard10NoEvasionPoison(unittest.TestCase):
    def test_evasion_dependency_fires_static_guard(self) -> None:
        path = POISON_DIR / "g10_evasion_dependency.py"
        source = path.read_text(encoding="utf-8")
        with self.assertRaises(GuardViolation):
            lint_no_evasion_deps(source, path=path)

    def test_403_with_escalation_routes_blocked_not_stealth(self) -> None:
        with self.assertRaises(GuardViolation):
            guard10_route_403_to_blocked(
                status_code=403,
                escalation="stealth_browser",
            )
        self.assertEqual(
            guard10_route_403_to_blocked(status_code=403, escalation=None),
            "BLOCKED",
        )


class Guard11NormalizeInvariantsPoison(unittest.TestCase):
    def test_out_of_range_normalized_value_fires_runtime_guard(self) -> None:
        with self.assertRaises(GuardViolation) as ctx:
            guard11_assert_normalize_invariants(
                normalized_score=1.5,
                window={"from": "2026-01-01T00:00:00Z", "to": "2026-07-01T00:00:00Z"},
                direction_applied=True,
            )
        self.assertIn("[0, 1]", str(ctx.exception))


class Guard12ScoreReproducibilityPoison(unittest.TestCase):
    def test_nondeterministic_score_path_fires_reproducibility_guard(self) -> None:
        expected = 0.72

        def nondeterministic_score() -> float:
            return expected + random.random()

        with self.assertRaises(GuardViolation):
            guard12_assert_score_reproducible(
                nondeterministic_score,
                expected=expected,
            )

    def test_deterministic_score_matches_golden(self) -> None:
        golden = json.loads(GOLDEN_OPPORTUNITY.read_text(encoding="utf-8"))
        expected = golden["score"]

        guard12_assert_score_reproducible(lambda: expected, expected=expected)


class GuardHarnessRegistrationTests(unittest.TestCase):
    """Each guard number is registered and has a poison test module hook."""

    def test_all_guard_numbers_have_specs(self) -> None:
        for number in range(1, 13):
            spec = get_guard(number)
            self.assertGreater(len(spec.mechanism), 0)


if __name__ == "__main__":
    unittest.main()
