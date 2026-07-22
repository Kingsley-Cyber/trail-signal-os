"""Typed graph node executor — bounded loop, packed input, no hooks (N12)."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from guards.schema_guards import load_schema, validate_instance
from harness.gateway import LLMGateway

# Agent Zero plugin hooks and ambient context are intentionally excluded (ADR-001).
_HOOK_MARKERS = frozenset(
    {
        "register_hook",
        "HookRegistry",
        "plugin_hooks",
        "run_hooks",
        "hook_manager",
    }
)

FORBIDDEN_CONTEXT_KEYS = frozenset(
    {
        "hooks",
        "transcript",
        "conversation_history",
        "shared_context",
        "memory",
        "terminal",
        "spawn",
        "tool_registry",
        "plugin_state",
    }
)

LAW1_FORBIDDEN_LLM_OUTPUT_SCHEMAS = frozenset(
    {
        "opportunity.v1.schema.json",
        "opportunity.v1",
    }
)


class NodeKind(str, Enum):
    LLM = "llm"
    DETERMINISTIC = "deterministic"


class NodeExecutorError(Exception):
    """Base node executor error."""


class HookInjectionError(NodeExecutorError):
    """Hook or plugin injection was attempted."""


class PackedInputError(NodeExecutorError):
    """Packed input failed schema or context isolation checks."""


class MissingVerifierError(NodeExecutorError):
    """Node definition lacks a verifier."""


class Law1ViolationError(NodeExecutorError):
    """LLM node attempted to emit a scored artifact."""


class IterationCeilingError(NodeExecutorError):
    """Verifier never passed within max_iterations."""


VerifierFn = Callable[[dict[str, Any], dict[str, Any]], "VerifierResult"]
DeterministicFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class NodeDefinition:
    """Runtime node contract (doc 07 §2)."""

    node_id: str
    kind: NodeKind
    input_schema: str
    output_schema: str
    verifier: VerifierFn
    max_iterations: int
    role: str | None = None
    prompt: str | None = None
    cassette_kind: str | None = None

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if self.kind is NodeKind.LLM and not self.role:
            raise ValueError("llm nodes require role")
        if self.verifier is None:
            raise MissingVerifierError(f"node {self.node_id!r} requires a verifier")
        if self.kind is NodeKind.LLM and _schema_name(self.output_schema) in LAW1_FORBIDDEN_LLM_OUTPUT_SCHEMAS:
            raise Law1ViolationError(
                f"node {self.node_id!r}: LLM nodes must not output opportunity scores (LAW 1)"
            )


@dataclass(frozen=True)
class VerifierResult:
    passed: bool
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeExecutionResult:
    node_id: str
    verdict: str
    attempts: int
    output: dict[str, Any] | None
    violations: tuple[str, ...]
    replayed: bool | None = None


def _schema_name(schema_ref: str) -> str:
    if schema_ref.endswith(".schema.json"):
        return schema_ref
    if schema_ref.endswith(".v1"):
        return f"{schema_ref}.schema.json"
    return schema_ref


def hooks_are_stripped() -> bool:
    """Return True when executor source defines no Agent Zero hook machinery."""
    source = __file__
    from pathlib import Path

    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in _HOOK_MARKERS:
                return False
    return True


def reject_hook_injection(payload: dict[str, Any] | None, *, label: str = "payload") -> None:
    """Reject hook keys anywhere in a mapping."""
    if payload is None:
        return
    if "hooks" in payload:
        raise HookInjectionError(f"{label} must not contain hooks")


def validate_packed_input(packed_input: dict[str, Any], input_schema: str) -> None:
    """Validate typed input and reject ambient context keys."""
    if not isinstance(packed_input, dict):
        raise PackedInputError("packed input must be a JSON object")
    reject_hook_injection(packed_input, label="packed_input")
    forbidden = FORBIDDEN_CONTEXT_KEYS.intersection(packed_input.keys())
    if forbidden:
        raise PackedInputError(
            f"packed input contains forbidden context keys: {', '.join(sorted(forbidden))}"
        )
    schema_name = _schema_name(input_schema)
    try:
        validate_instance(schema_name, packed_input)
    except jsonschema.ValidationError as exc:
        raise PackedInputError(str(exc.message)) from exc


def schema_validate_verifier(output_schema: str) -> VerifierFn:
    """Deterministic verifier: output must validate against output_schema."""

    schema_name = _schema_name(output_schema)
    validator = Draft202012Validator(load_schema(schema_name))

    def _verify(output: dict[str, Any], _packed_input: dict[str, Any]) -> VerifierResult:
        try:
            validator.validate(output)
        except jsonschema.ValidationError as exc:
            return VerifierResult(passed=False, violations=(exc.message,))
        return VerifierResult(passed=True)

    return _verify


def _validate_output_schema(output: dict[str, Any], output_schema: str) -> None:
    validate_instance(_schema_name(output_schema), output)


def _build_messages(
    node: NodeDefinition,
    packed_input: dict[str, Any],
    *,
    prior_output: dict[str, Any] | None = None,
    violations: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    prompt = node.prompt or f"Execute node {node.node_id} on the packed input artifact."
    user_content = {
        "node_id": node.node_id,
        "input_schema": node.input_schema,
        "output_schema": node.output_schema,
        "packed_input": packed_input,
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(user_content, sort_keys=True)},
    ]
    if prior_output is not None and violations:
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(prior_output, sort_keys=True),
            }
        )
        messages.append(
            {
                "role": "user",
                "content": "Repair the output. Verifier violations:\n"
                + "\n".join(f"- {item}" for item in violations),
            }
        )
    return messages


def _extract_llm_output(result_parsed: dict[str, Any] | None, text: str) -> dict[str, Any]:
    if isinstance(result_parsed, dict):
        return dict(result_parsed)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NodeExecutorError(f"LLM output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise NodeExecutorError("LLM output must be a JSON object")
    return payload


def execute_node(
    node: NodeDefinition,
    packed_input: dict[str, Any],
    *,
    gateway: LLMGateway | None = None,
    deterministic_fn: DeterministicFn | None = None,
    replay_request: dict[str, Any] | None = None,
) -> NodeExecutionResult:
    """Execute one typed node against packed input only."""
    reject_hook_injection(replay_request, label="replay_request")
    validate_packed_input(packed_input, node.input_schema)

    if node.kind is NodeKind.DETERMINISTIC:
        if deterministic_fn is None:
            raise NodeExecutorError(f"deterministic node {node.node_id!r} requires deterministic_fn")
        output = deterministic_fn(packed_input)
        _validate_output_schema(output, node.output_schema)
        verdict = node.verifier(output, packed_input)
        if not verdict.passed:
            raise IterationCeilingError(
                f"deterministic node {node.node_id!r} failed verifier: {', '.join(verdict.violations)}"
            )
        return NodeExecutionResult(
            node_id=node.node_id,
            verdict="pass",
            attempts=1,
            output=output,
            violations=(),
            replayed=None,
        )

    llm_gateway = gateway or LLMGateway()
    last_violations: tuple[str, ...] = ()
    last_output: dict[str, Any] | None = None
    replayed: bool | None = None

    for attempt in range(1, node.max_iterations + 1):
        messages = _build_messages(
            node,
            packed_input,
            prior_output=last_output if attempt > 1 else None,
            violations=last_violations if attempt > 1 else (),
        )
        for message in messages:
            reject_hook_injection(message, label="message")

        completion = llm_gateway.generate(
            node.role,
            messages,
            cassette_kind=node.cassette_kind,
            replay_request=replay_request,
        )
        replayed = completion.replayed
        candidate = _extract_llm_output(completion.parsed, completion.text)
        _validate_output_schema(candidate, node.output_schema)
        verdict = node.verifier(candidate, packed_input)
        if verdict.passed:
            return NodeExecutionResult(
                node_id=node.node_id,
                verdict="pass",
                attempts=attempt,
                output=candidate,
                violations=(),
                replayed=replayed,
            )
        last_output = candidate
        last_violations = verdict.violations

    return NodeExecutionResult(
        node_id=node.node_id,
        verdict="ceiling",
        attempts=node.max_iterations,
        output=last_output,
        violations=last_violations,
        replayed=replayed,
    )


__all__ = [
    "DeterministicFn",
    "FORBIDDEN_CONTEXT_KEYS",
    "HookInjectionError",
    "IterationCeilingError",
    "Law1ViolationError",
    "MissingVerifierError",
    "NodeDefinition",
    "NodeExecutionResult",
    "NodeExecutorError",
    "NodeKind",
    "PackedInputError",
    "VerifierFn",
    "VerifierResult",
    "execute_node",
    "hooks_are_stripped",
    "reject_hook_injection",
    "schema_validate_verifier",
    "validate_packed_input",
]
