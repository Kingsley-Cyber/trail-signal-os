"""N1 schemas — JSON Schema round-trip tests (schema-round-trip verifier)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"

N1_SCHEMAS = [
    "page.v1.schema.json",
    "evidence.v1.schema.json",
    "signal.v1.schema.json",
    "opportunity.v1.schema.json",
    "decision.v1.schema.json",
    "job.v1.schema.json",
    "task.v1.schema.json",
    "budget.v1.schema.json",
    "domain_profile.v1.schema.json",
    "degradation_event.v1.schema.json",
]

CONFIG_HASH = "sha256:" + ("a" * 64)
CONTENT_HASH = "sha256:" + ("b" * 64)
IDEMPOTENCY_KEY = "sha256:" + ("c" * 64)
CREATED_AT = "2026-07-21T12:00:00Z"

BUDGET = {
    "max_queries": 30,
    "max_fetched_urls": 2000,
    "per_domain_urls": 300,
    "browser_pages": 60,
    "media_items": 150,
    "max_bytes": 5368709120,
    "deadline_minutes": 45,
    "max_attempts": 4,
    "llm_budget": {"max_calls": 500, "max_tokens": 2000000, "max_usd": 0},
    "schema_version": "budget.v1",
}

MINIMAL_VALID: dict[str, dict] = {
    "page.v1.schema.json": {
        "page_id": "pg_01JTEST",
        "url": "https://example.com/article",
        "canonical_url": "https://example.com/article",
        "domain": "example.com",
        "fetched_at": CREATED_AT,
        "title": "Sample article",
        "text_md": "# Sample\n\nBody text.",
        "links": [],
        "media": [],
        "platform_fields": {},
        "content_hash": CONTENT_HASH,
        "derived_from": ["tsk_01JFETCH"],
        "provenance": {
            "code_version": "extract-1.0.0",
            "schema_version": "page.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "page.v1",
    },
    "evidence.v1.schema.json": {
        "record_id": "ev_01JTEST",
        "source": {
            "url": "https://example.com/article",
            "domain": "example.com",
        },
        "evidence_type": "behavior",
        "polarity": "supporting",
        "observation": "Users report repeated workaround behavior.",
        "retrieved_at": "2026-07-21",
        "independence_group": "example.com:article",
        "confidence": "medium",
        "derived_from": ["pg_01JTEST"],
        "content_hash": CONTENT_HASH,
        "extraction": {
            "model_id": "qwen3-4b-q4",
            "prompt_version": "enrich_page-2026.07.21",
        },
        "provenance": {
            "model_id": "qwen3-4b-q4",
            "prompt_version": "enrich_page-2026.07.21",
            "schema_version": "evidence.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "evidence.v1",
    },
    "signal.v1.schema.json": {
        "signal_id": "sig_01JTEST",
        "niche_id": "camping",
        "signal_type": "demand",
        "source": {"domain": "example.com", "tier": "open"},
        "window": {"from": "2026-01-01T00:00:00Z", "to": "2026-07-01T00:00:00Z"},
        "normalized_score": 0.72,
        "confidence": 0.8,
        "observed_at": CREATED_AT,
        "expires_at": "2026-09-21T12:00:00Z",
        "derived_from": ["ev_01JTEST"],
        "provenance": {
            "code_version": "normalize-1.0.0",
            "schema_version": "signal.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "signal.v1",
    },
    "opportunity.v1.schema.json": {
        "opportunity_id": "opp_01JTEST",
        "niche_id": "camping",
        "candidate": {"candidate_id": "nc-001", "title": "Portable camping fan"},
        "score": 0.72,
        "subscores": {
            "demand": 0.68,
            "growth": 0.58,
            "pain": 0.71,
            "competition": 0.55,
            "content": 0.62,
        },
        "confidence": 0.74,
        "coverage_gaps": [],
        "scored_from": ["sig_01JTEST"],
        "generating_queries": ["qsp_01JQUERY"],
        "provenance": {
            "scoring_version": "score-1.0.0",
            "weights_version": "w-2026.07.21",
            "normalize_version": "normalize-1.0.0",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "as_of": CREATED_AT,
        "schema_version": "opportunity.v1",
    },
    "decision.v1.schema.json": {
        "decision_id": "dec_01JTEST",
        "action": "expand",
        "args": {"niche_id": "camping", "max_queries": 10},
        "rationale": {
            "text": "Coverage gaps remain in pain signals.",
            "provenance": {
                "model_id": "qwen3-4b-q4",
                "prompt_version": "planner-2026.07.21",
            },
        },
        "cited_manifest_hash": CONFIG_HASH,
        "derived_from": ["opp_01JTEST"],
        "provenance": {
            "code_version": "decide-1.0.0",
            "schema_version": "decision.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "decision.v1",
    },
    "job.v1.schema.json": {
        "job_id": "job_01JTEST",
        "job_kind": "dossier",
        "status": "CREATED",
        "budget": BUDGET,
        "config_hash": CONFIG_HASH,
        "provenance": {
            "schema_version": "job.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "job.v1",
    },
    "task.v1.schema.json": {
        "task_id": "tsk_01JTEST",
        "job_id": "job_01JTEST",
        "lane": "http",
        "priority": 2,
        "attempt": 1,
        "status": "READY",
        "idempotency_key": IDEMPOTENCY_KEY,
        "payload_ref": "postgres://tasks/tsk_01JTEST",
        "created_at": CREATED_AT,
        "provenance": {
            "schema_version": "task.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "task.v1",
    },
    "budget.v1.schema.json": BUDGET,
    "domain_profile.v1.schema.json": {
        "domain": "example.com",
        "tier": "generic",
        "requires_browser": False,
        "supports_http": True,
        "max_concurrency": 4,
        "last_verified_at": CREATED_AT,
        "profile_ttl_days": 14,
        "provenance": {
            "code_version": "routing-1.0.0",
            "schema_version": "domain_profile.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "domain_profile.v1",
    },
    "degradation_event.v1.schema.json": {
        "event_id": "deg_01JTEST",
        "domain": "youtube.com",
        "route": "youtube:ytdlp",
        "event_type": "circuit_open",
        "failure_class": "HTTP_429",
        "cooldown_until": "2026-07-22T00:00:00Z",
        "recorded_at": CREATED_AT,
        "provenance": {
            "code_version": "circuits-1.0.0",
            "schema_version": "degradation_event.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "degradation_event.v1",
    },
}

KNOWN_INVALID: dict[str, dict] = {
    "page.v1.schema.json": {"page_id": "bad-id", "schema_version": "page.v1"},
    "evidence.v1.schema.json": {
        "record_id": "ev_01JTEST",
        "source": {"url": "https://example.com", "domain": "example.com"},
        "evidence_type": "behavior",
        "polarity": "maybe",
        "observation": "x",
        "retrieved_at": "2026-07-21",
        "independence_group": "g",
        "confidence": "medium",
        "derived_from": ["pg_01JTEST"],
        "content_hash": CONTENT_HASH,
        "extraction": {"model_id": "m", "prompt_version": "p"},
        "provenance": {
            "model_id": "m",
            "prompt_version": "p",
            "schema_version": "evidence.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "evidence.v1",
    },
    "signal.v1.schema.json": {
        "signal_id": "sig_01JTEST",
        "niche_id": "camping",
        "signal_type": "demand",
        "source": {"domain": "example.com", "tier": "open"},
        "window": {"from": CREATED_AT, "to": CREATED_AT},
        "normalized_score": 1.5,
        "confidence": 0.8,
        "observed_at": CREATED_AT,
        "expires_at": CREATED_AT,
        "derived_from": ["ev_01JTEST"],
        "provenance": {
            "code_version": "normalize-1.0.0",
            "schema_version": "signal.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "signal.v1",
    },
    "opportunity.v1.schema.json": {
        "opportunity_id": "opp_01JTEST",
        "niche_id": "camping",
        "candidate": {"candidate_id": "nc-001", "title": "Fan"},
        "score": 0.72,
        "subscores": {
            "demand": 0.68,
            "growth": 0.58,
            "pain": 0.71,
            "competition": 0.55,
            "content": 0.62,
        },
        "confidence": 0.74,
        "coverage_gaps": [],
        "scored_from": ["sig_01JTEST"],
        "generating_queries": [],
        "provenance": {
            "scoring_version": "score-1.0.0",
            "weights_version": "w-2026.07.21",
            "normalize_version": "normalize-1.0.0",
            "model_id": "llm-should-not-score",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "as_of": CREATED_AT,
        "schema_version": "opportunity.v1",
    },
    "decision.v1.schema.json": {
        "decision_id": "dec_01JTEST",
        "action": "pause",
        "args": {},
        "rationale": {
            "text": "x",
            "provenance": {"model_id": "m", "prompt_version": "p"},
        },
        "cited_manifest_hash": CONFIG_HASH,
        "derived_from": ["opp_01JTEST"],
        "provenance": {
            "schema_version": "decision.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "decision.v1",
    },
    "job.v1.schema.json": {
        "job_id": "job_01JTEST",
        "job_kind": "dossier",
        "status": "CREATED",
        "budget": BUDGET,
        "config_hash": CONFIG_HASH,
        "provenance": {
            "schema_version": "job.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "page.v1",
    },
    "task.v1.schema.json": {
        "task_id": "tsk_01JTEST",
        "job_id": "job_01JTEST",
        "lane": "http",
        "priority": 2,
        "attempt": 0,
        "status": "READY",
        "idempotency_key": "not-a-hash",
        "payload_ref": "postgres://tasks/tsk_01JTEST",
        "created_at": CREATED_AT,
        "provenance": {
            "schema_version": "task.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "task.v1",
    },
    "budget.v1.schema.json": {
        **BUDGET,
        "deadline_minutes": 0,
    },
    "domain_profile.v1.schema.json": {
        "domain": "example.com",
        "tier": "premium",
        "requires_browser": False,
        "supports_http": True,
        "max_concurrency": 4,
        "last_verified_at": CREATED_AT,
        "profile_ttl_days": 14,
        "provenance": {
            "schema_version": "domain_profile.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "domain_profile.v1",
    },
    "degradation_event.v1.schema.json": {
        "event_id": "deg_01JTEST",
        "domain": "youtube.com",
        "route": "youtube:ytdlp",
        "event_type": "circuit_open",
        "recorded_at": CREATED_AT,
        "provenance": {
            "schema_version": "degradation_event.v1",
            "config_hash": CONFIG_HASH,
            "created_at": CREATED_AT,
        },
        "schema_version": "degradation_event.v1",
        "extra_field": True,
    },
}


def _load_schema(name: str) -> dict:
    path = SCHEMAS_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


class SchemaLoadTests(unittest.TestCase):
    def test_all_n1_schema_files_exist(self) -> None:
        for name in N1_SCHEMAS:
            path = SCHEMAS_DIR / name
            self.assertTrue(path.is_file(), f"missing {path}")

    def test_each_schema_is_valid_json_schema(self) -> None:
        for name in N1_SCHEMAS:
            schema = _load_schema(name)
            Draft202012Validator.check_schema(schema)


class SchemaRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validators = {
            name: Draft202012Validator(_load_schema(name))
            for name in N1_SCHEMAS
        }

    def test_minimal_valid_instance_passes_for_each_schema(self) -> None:
        for name in N1_SCHEMAS:
            with self.subTest(schema=name):
                instance = MINIMAL_VALID[name]
                self.validators[name].validate(instance)

    def test_known_invalid_instance_fails_for_each_schema(self) -> None:
        for name in N1_SCHEMAS:
            with self.subTest(schema=name):
                instance = KNOWN_INVALID[name]
                with self.assertRaises(jsonschema.ValidationError):
                    self.validators[name].validate(instance)


class IntegrationCheckSchemas(unittest.TestCase):
    """Offline integration check for N1 schema-round-trip verifier."""

    def test_ten_schemas_round_trip(self) -> None:
        self.assertEqual(len(N1_SCHEMAS), 10)
        for name in N1_SCHEMAS:
            validator = Draft202012Validator(_load_schema(name))
            validator.validate(MINIMAL_VALID[name])
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate(KNOWN_INVALID[name])


if __name__ == "__main__":
    unittest.main()
