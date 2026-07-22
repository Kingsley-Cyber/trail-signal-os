"""Catalog of the 12 invariant guards (doc 09 §1)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuardSpec:
    number: int
    name: str
    invariant: str
    guard_type: str
    mechanism: str
    source: str


GUARD_CATALOG: tuple[GuardSpec, ...] = (
    GuardSpec(
        1,
        "ack_after_commit",
        "Ack after commit (v1 §7)",
        "static",
        "Lint bans raw `.xack(` outside `process_task()` commit→XACK template",
        "docs/build/09_verification_harness.md §1 #1",
    ),
    GuardSpec(
        2,
        "fencing_token",
        "Fencing token on result writes (v1 §6)",
        "static+runtime",
        "Task state updates require lease_owner+lease_generation; 0-row update raises StaleLeaseError",
        "docs/build/09_verification_harness.md §1 #2",
    ),
    GuardSpec(
        3,
        "idempotency_unique",
        "Idempotency keys unique (v1 §15, v4 §8)",
        "schema",
        "DB unique constraints on task/signal/opportunity keys; duplicate insert is no-op",
        "docs/build/09_verification_harness.md §1 #3",
    ),
    GuardSpec(
        4,
        "outbox_atomicity",
        "Outbox atomicity+ordering (v1 §2)",
        "static+runtime",
        "No `XADD` to `cp:*` outside `dispatcher/`; task+outbox in one transaction",
        "docs/build/09_verification_harness.md §1 #4",
    ),
    GuardSpec(
        5,
        "law1_no_llm_score",
        "LAW 1 — no LLM score (v4 §0)",
        "static+runtime",
        "Import purity for signal_engine; write guard rejects score provenance containing model_id",
        "docs/build/09_verification_harness.md §1 #5",
    ),
    GuardSpec(
        6,
        "law2_total_lineage",
        "LAW 2 — total lineage (v4 §6)",
        "runtime",
        "Derived artifacts require non-empty parent refs and a lineage_edges row",
        "docs/build/09_verification_harness.md §1 #6",
    ),
    GuardSpec(
        7,
        "provenance_stamp",
        "Provenance stamp on every artifact (v3 §5)",
        "static+runtime",
        "Artifact writes go through persist_artifact(provenance=…); lint bans direct artifact inserts",
        "docs/build/09_verification_harness.md §1 #7",
    ),
    GuardSpec(
        8,
        "workflow_verifier",
        "Every LLM node has a verifier; back-edges bounded (07 §2/§4)",
        "schema",
        "Workflow compile-time schema requires verifier for kind: llm and max_trips on back-edges",
        "docs/build/09_verification_harness.md §1 #8",
    ),
    GuardSpec(
        9,
        "deterministic_import_purity",
        "Deterministic modules are import-pure (08)",
        "static",
        "Import-graph assertion over signal_engine normalize|score|confidence|coverage|tiers",
        "docs/build/09_verification_harness.md §1 #9",
    ),
    GuardSpec(
        10,
        "no_evasion",
        "No-evasion (06 §2.7)",
        "static+runtime",
        "Dependency denylist; HTTP 403 routes to BLOCKED, never browser escalation",
        "docs/build/09_verification_harness.md §1 #10",
    ),
    GuardSpec(
        11,
        "normalize_invariants",
        "Normalize invariants (08 §4)",
        "runtime",
        "Assert 0≤s≤1, window set, direction applied; out-of-range is hard error",
        "docs/build/09_verification_harness.md §1 #11",
    ),
    GuardSpec(
        12,
        "score_reproducibility",
        "Score reproducibility (v4 §13, 08 §7)",
        "test",
        "score() over fixture signals equals golden constant across two runs",
        "docs/build/09_verification_harness.md §1 #12",
    ),
)
