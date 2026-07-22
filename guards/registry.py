"""Guard registry and lookup."""

from __future__ import annotations

from guards.catalog import GUARD_CATALOG, GuardSpec


def list_guards() -> tuple[GuardSpec, ...]:
    return GUARD_CATALOG


def get_guard(number: int) -> GuardSpec:
    for spec in GUARD_CATALOG:
        if spec.number == number:
            return spec
    raise KeyError(f"unknown guard number: {number}")
