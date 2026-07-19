from __future__ import annotations
import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass
class ValidationResult:
    errors: list[str]
    warnings: list[str]
    stats: dict[str, int]

    @property
    def ok(self) -> bool:
        return not self.errors


REQUIRED_FILES = [
    "README.md", "AGENTS.md", "data/outdoor_activity_niche_seed.csv",
    "data/niche_candidates.csv", "data/research_evidence.csv",
    "data/source_registry.csv", "config/scoring_weights.json",
]
REQUIRED_HEADERS = {
    "data/outdoor_activity_niche_seed.csv": [
        "seed_id", "activity_id", "domain", "activity", "task",
        "friction_family", "fact_status", "research_status"
    ],
    "data/niche_candidates.csv": [
        "candidate_id", "activity_id", "candidate_title", "product_hypothesis",
        "research_state", "hard_gates_passed"
    ],
    "data/research_evidence.csv": [
        "evidence_id", "candidate_id", "evidence_type", "polarity",
        "source_id", "observation", "retrieved_at", "independence_group"
    ],
    "data/source_registry.csv": [
        "source_id", "source_name", "access_mode", "limitations",
        "last_registry_verification"
    ],
}
SCORE_FIELDS = [
    "behavior_frequency", "friction_severity", "workaround_strength",
    "complaint_repetition", "product_simplicity", "shipping_fit",
    "margin_potential", "return_risk_inverse", "community_reachability",
    "competition_gap", "seasonal_timing", "expansion_potential", "risk_inverse"
]


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _duplicates(rows: list[dict[str, str]], field: str) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        value = row.get(field, "")
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def validate_repository(root: Path) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict[str, int] = {}

    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            errors.append(f"Missing required file: {rel}")
    if errors:
        return ValidationResult(errors, warnings, stats)

    datasets: dict[str, list[dict[str, str]]] = {}
    for rel, required in REQUIRED_HEADERS.items():
        headers, rows = _read(root / rel)
        datasets[rel] = rows
        stats[rel] = len(rows)
        missing = [h for h in required if h not in headers]
        if missing:
            errors.append(f"{rel}: missing headers {missing}")

    seed = datasets["data/outdoor_activity_niche_seed.csv"]
    candidates = datasets["data/niche_candidates.csv"]
    evidence = datasets["data/research_evidence.csv"]
    sources = datasets["data/source_registry.csv"]

    checks = [
        ("seed_id", seed, "seed", r"^seed-\d{4,}$"),
        ("candidate_id", candidates, "candidate", r"^nc-\d{3,}$"),
        ("source_id", sources, "source", r"^src-[a-z0-9-]+$"),
    ]
    for field, rows, label, pattern in checks:
        duplicates = _duplicates(rows, field)
        if duplicates:
            errors.append(f"Duplicate {label} IDs: {duplicates[:5]}")
        invalid = [r.get(field, "") for r in rows if not re.match(pattern, r.get(field, ""))]
        if invalid:
            errors.append(f"Invalid {label} IDs: {invalid[:5]}")

    activity_ids = {r["activity_id"] for r in seed}
    missing_activities = sorted({
        r["activity_id"] for r in candidates if r.get("activity_id") not in activity_ids
    })
    if missing_activities:
        errors.append(f"Candidate activity IDs absent from seed: {missing_activities[:10]}")

    candidate_ids = {r["candidate_id"] for r in candidates}
    source_ids = {r["source_id"] for r in sources}
    for row in evidence:
        if row.get("candidate_id") not in candidate_ids:
            errors.append(f"Evidence {row.get('evidence_id')} has unknown candidate")
        if row.get("source_id") not in source_ids:
            errors.append(f"Evidence {row.get('evidence_id')} has unknown source")
        if row.get("polarity") not in {"supporting", "contradicting", "neutral"}:
            errors.append(f"Evidence {row.get('evidence_id')} invalid polarity")

    for row in candidates:
        for field in SCORE_FIELDS:
            try:
                value = int(row.get(field, ""))
            except ValueError:
                errors.append(f"{row.get('candidate_id')}: {field} is not integer")
                continue
            if value < 0 or value > 5:
                errors.append(f"{row.get('candidate_id')}: {field} outside 0-5")
        if row.get("fact_status") != "hypothesis" and not row.get("evidence_ids"):
            warnings.append(f"{row.get('candidate_id')}: non-hypothesis candidate has no evidence IDs")
        if row.get("hard_gates_passed", "").lower() not in {"true", "false"}:
            errors.append(f"{row.get('candidate_id')}: hard_gates_passed must be true/false")

    json_files = [
        "config/scoring_weights.json", "config/evidence_gates.json",
        "config/seasonality_rules.json", "config/research_run_states.json"
    ]
    for rel in json_files:
        try:
            json.loads((root / rel).read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"Invalid JSON {rel}: {exc}")

    today = date.today()
    for row in sources:
        raw = row.get("last_registry_verification", "")
        try:
            age = (today - date.fromisoformat(raw)).days
            if age > 180:
                warnings.append(f"Source registry stale ({age} days): {row.get('source_id')}")
        except ValueError:
            errors.append(f"Invalid source verification date: {row.get('source_id')}={raw}")

    if len(seed) < 500:
        warnings.append("Seed corpus has fewer than 500 activity-task rows")
    if len(candidates) < 20:
        warnings.append("Candidate queue has fewer than 20 candidates")
    return ValidationResult(errors, warnings, stats)
