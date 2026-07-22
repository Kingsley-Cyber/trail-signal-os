"""claim_grounding — cited record_ids exist; quoted numbers match (doc 07 §4)."""

from __future__ import annotations

import re
from typing import Any

from graph.verifiers.base import VerifierFn, VerifierResult

_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


def _record_metric_value(record: dict[str, Any]) -> str | None:
    metric_value = record.get("metric_value")
    if metric_value is None:
        return None
    return str(metric_value)


def claim_grounding() -> VerifierFn:
    """Validate synthesis claims against the evidence store in packed_input."""

    def _verify(output: dict[str, Any], packed_input: dict[str, Any]) -> VerifierResult:
        violations: list[str] = []
        evidence_store = packed_input.get("evidence_store")
        if not isinstance(evidence_store, dict):
            return VerifierResult(
                passed=False,
                violations=("packed_input.evidence_store must be a record_id map",),
            )

        claims = output.get("claims")
        if not isinstance(claims, list) or not claims:
            return VerifierResult(passed=False, violations=("synthesis.claims must be a non-empty list",))

        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                violations.append(f"claims[{index}] must be an object")
                continue

            cited_ids = claim.get("cited_record_ids")
            if not isinstance(cited_ids, list) or not cited_ids:
                violations.append(f"claims[{index}].cited_record_ids must be non-empty")
                continue

            for record_id in cited_ids:
                if record_id not in evidence_store:
                    violations.append(f"claims[{index}] cites unknown record_id {record_id!r}")

            numbers = claim.get("numbers") or []
            if not isinstance(numbers, list):
                violations.append(f"claims[{index}].numbers must be a list when present")
                continue

            for number_index, number in enumerate(numbers):
                if not isinstance(number, dict):
                    violations.append(f"claims[{index}].numbers[{number_index}] must be an object")
                    continue
                record_id = number.get("record_id")
                value = number.get("value")
                if not isinstance(record_id, str) or record_id not in evidence_store:
                    violations.append(
                        f"claims[{index}].numbers[{number_index}] record_id {record_id!r} not in evidence store"
                    )
                    continue
                expected = _record_metric_value(evidence_store[record_id])
                if expected is None:
                    violations.append(
                        f"claims[{index}].numbers[{number_index}] record {record_id!r} has no metric_value"
                    )
                    continue
                if str(value) != expected:
                    violations.append(
                        f"claims[{index}].numbers[{number_index}] value {value!r} != evidence metric {expected!r}"
                    )

            claim_text = claim.get("text")
            if isinstance(claim_text, str):
                for match in _NUMBER_PATTERN.findall(claim_text):
                    if not any(str(item.get("value")) == match for item in numbers if isinstance(item, dict)):
                        violations.append(
                            f"claims[{index}] text quotes number {match} without a grounded numbers entry"
                        )

        return VerifierResult(passed=not violations, violations=tuple(violations))

    return _verify
