"""Shared verifier types (doc 07 §4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

VerifierFn = Callable[[dict[str, Any], dict[str, Any]], "VerifierResult"]


@dataclass(frozen=True)
class VerifierResult:
    passed: bool
    violations: tuple[str, ...] = ()
