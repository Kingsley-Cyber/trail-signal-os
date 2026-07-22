"""plan_validates — budget caps and platform allowlist (doc 07 §4)."""

from __future__ import annotations

from typing import Any

from graph.verifiers.base import VerifierFn, VerifierResult

DEFAULT_PLATFORM_ALLOWLIST = frozenset(
    {"web", "reddit", "youtube", "forum", "amazon", "media", "search"}
)


def _budget_limit(budget: dict[str, Any], key: str) -> int | None:
    value = budget.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        return None
    return value


def plan_validates(
    *,
    platform_allowlist: frozenset[str] | None = None,
) -> VerifierFn:
    """Validate planner output against job budgets and allowed platforms."""
    allowlist = platform_allowlist or DEFAULT_PLATFORM_ALLOWLIST

    def _verify(output: dict[str, Any], packed_input: dict[str, Any]) -> VerifierResult:
        violations: list[str] = []
        queries = output.get("queries")
        if not isinstance(queries, list) or not queries:
            return VerifierResult(passed=False, violations=("plan.queries must be a non-empty list",))

        budget = packed_input.get("budget")
        if not isinstance(budget, dict):
            return VerifierResult(passed=False, violations=("packed_input.budget is required",))

        max_queries = _budget_limit(budget, "max_queries")
        if max_queries is not None and len(queries) > max_queries:
            violations.append(
                f"plan has {len(queries)} queries but budget.max_queries is {max_queries}"
            )

        for index, query in enumerate(queries):
            if not isinstance(query, dict):
                violations.append(f"queries[{index}] must be an object")
                continue
            platform = query.get("platform")
            if not isinstance(platform, str) or not platform:
                violations.append(f"queries[{index}].platform is required")
                continue
            if platform not in allowlist:
                violations.append(
                    f"queries[{index}].platform {platform!r} not in allowlist"
                )
            text = query.get("text")
            if not isinstance(text, str) or not text.strip():
                violations.append(f"queries[{index}].text is required")

        return VerifierResult(passed=not violations, violations=tuple(violations))

    return _verify
