"""decision_valid — enum, args, and manifest hash checks (doc 07 §4)."""

from __future__ import annotations

from typing import Any

from graph.verifiers.base import VerifierFn, VerifierResult
from graph.verifiers.schema_validate import schema_validate

_ACTION_ARG_REQUIREMENTS: dict[str, frozenset[str]] = {
    "expand": frozenset({"niche_id"}),
    "stop": frozenset(),
    "synthesize": frozenset({"synthesis_id"}),
    "escalate": frozenset({"reason"}),
}


def decision_valid() -> VerifierFn:
    """Validate decision.v1 output and cited manifest hash against packed input."""
    schema_verifier = schema_validate("decision.v1")

    def _verify(output: dict[str, Any], packed_input: dict[str, Any]) -> VerifierResult:
        schema_result = schema_verifier(output, packed_input)
        if not schema_result.passed:
            return schema_result

        violations: list[str] = []
        manifest_hash = packed_input.get("manifest_hash")
        cited = output.get("cited_manifest_hash")
        if not isinstance(manifest_hash, str) or not manifest_hash:
            violations.append("packed_input.manifest_hash is required")
        elif cited != manifest_hash:
            violations.append(
                f"cited_manifest_hash {cited!r} != packed_input.manifest_hash {manifest_hash!r}"
            )

        action = output.get("action")
        args = output.get("args")
        if isinstance(action, str) and isinstance(args, dict):
            required = _ACTION_ARG_REQUIREMENTS.get(action)
            if required is None:
                violations.append(f"unsupported decision action {action!r}")
            else:
                missing = sorted(key for key in required if key not in args)
                if missing:
                    violations.append(
                        f"decision.args missing required keys for {action}: {', '.join(missing)}"
                    )
        else:
            violations.append("decision.action and decision.args are required")

        if violations:
            return VerifierResult(passed=False, violations=tuple(violations))
        return VerifierResult(passed=True)

    return _verify
