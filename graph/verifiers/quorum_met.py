"""quorum_met — fan-in SQL count thresholds (doc 07 §4)."""

from __future__ import annotations

from typing import Any

from graph.verifiers.base import VerifierFn, VerifierResult

DEFAULT_MIN_RECORDS = 100
DEFAULT_MIN_DOMAINS = 10


def quorum_met(
    *,
    min_records: int = DEFAULT_MIN_RECORDS,
    min_domains: int = DEFAULT_MIN_DOMAINS,
) -> VerifierFn:
    """Validate rollup/quorum counts against fan-in thresholds."""

    def _verify(output: dict[str, Any], packed_input: dict[str, Any]) -> VerifierResult:
        quorum = packed_input.get("quorum")
        if isinstance(quorum, dict):
            counts = quorum
            required_records = int(quorum.get("min_records", min_records))
            required_domains = int(quorum.get("min_domains", min_domains))
        else:
            counts = output.get("quorum_counts") or output
            required_records = min_records
            required_domains = min_domains

        if not isinstance(counts, dict):
            return VerifierResult(
                passed=False,
                violations=("quorum counts must be provided in packed_input.quorum or output.quorum_counts",),
            )

        validated_records = counts.get("validated_records")
        domains = counts.get("domains")
        violations: list[str] = []

        if not isinstance(validated_records, int):
            violations.append("validated_records must be an integer count")
        elif validated_records < required_records:
            violations.append(
                f"validated_records {validated_records} < required {required_records}"
            )

        if not isinstance(domains, int):
            violations.append("domains must be an integer count")
        elif domains < required_domains:
            violations.append(f"domains {domains} < required {required_domains}")

        return VerifierResult(passed=not violations, violations=tuple(violations))

    return _verify
