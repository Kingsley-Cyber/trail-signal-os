"""schema_validate — output must validate against a JSON Schema (doc 07 §4)."""

from __future__ import annotations

from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from graph.verifiers.base import VerifierFn, VerifierResult
from guards.schema_guards import load_schema


def _schema_name(schema_ref: str) -> str:
    if schema_ref.endswith(".schema.json"):
        return schema_ref
    if schema_ref.endswith(".v1"):
        return f"{schema_ref}.schema.json"
    return schema_ref


def schema_validate(output_schema: str) -> VerifierFn:
    """Return a verifier that validates output against output_schema."""
    schema_name = _schema_name(output_schema)
    validator = Draft202012Validator(load_schema(schema_name))

    def _verify(output: dict[str, Any], _packed_input: dict[str, Any]) -> VerifierResult:
        try:
            validator.validate(output)
        except jsonschema.ValidationError as exc:
            return VerifierResult(passed=False, violations=(exc.message,))
        return VerifierResult(passed=True)

    return _verify
