"""sample_judge — optional 2% LLM QA on enrich outputs (doc 07 §4)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from graph.verifiers.base import VerifierFn, VerifierResult

DEFAULT_SAMPLE_RATE_PCT = 2
JUDGE_ROLE = "judge"


def _in_sample(record_id: str, sample_rate_pct: int) -> bool:
    digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < sample_rate_pct


def sample_judge(
    *,
    sample_rate_pct: int = DEFAULT_SAMPLE_RATE_PCT,
    gateway: Any | None = None,
) -> VerifierFn:
    """Judge a deterministic sample of enrich outputs via the judge gateway role."""

    def _verify(output: dict[str, Any], packed_input: dict[str, Any]) -> VerifierResult:
        record_id = output.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            return VerifierResult(passed=False, violations=("output.record_id is required",))

        rate = int(packed_input.get("sample_rate_pct", sample_rate_pct))
        if not _in_sample(record_id, rate):
            return VerifierResult(passed=True)

        if gateway is None:
            return VerifierResult(
                passed=False,
                violations=("sample selected but no judge gateway configured",),
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "Review the evidence artifact for factual grounding and schema fidelity. "
                    "Respond with JSON: {\"pass\": true|false, \"violations\": [\"...\"]}. "
                    "Do not emit scores."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"record_id": record_id, "evidence": output}, sort_keys=True),
            },
        ]
        completion = gateway.generate(JUDGE_ROLE, messages, cassette_kind="judge")
        parsed = completion.parsed if isinstance(completion.parsed, dict) else None
        if parsed is None:
            try:
                parsed = json.loads(completion.text)
            except json.JSONDecodeError:
                return VerifierResult(
                    passed=False,
                    violations=("judge output is not valid JSON",),
                )

        if parsed.get("pass") is True:
            return VerifierResult(passed=True)

        judge_violations = parsed.get("violations")
        if isinstance(judge_violations, list) and judge_violations:
            return VerifierResult(
                passed=False,
                violations=tuple(str(item) for item in judge_violations),
            )
        return VerifierResult(passed=False, violations=("judge rejected sample without violations",))

    return _verify
