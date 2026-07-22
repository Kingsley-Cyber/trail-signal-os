"""Static lint guards (doc 09 §1 guards 1, 4, 7, 9, 10)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from guards.exceptions import GuardViolation

PROCESS_TASK_TEMPLATE = "process_task"
XADD_PATTERN = re.compile(r"\bxadd\b.*\bcp:", re.IGNORECASE)
ARTIFACT_INSERT_PATTERN = re.compile(
    r"\bINSERT\s+INTO\s+artifacts\b", re.IGNORECASE
)
FORBIDDEN_IMPORT_PREFIXES = (
    "harness.gateway",
    "niche_research.gateway",
    "requests",
    "httpx",
    "curl_cffi",
)
DETERMINISTIC_MODULE_NAMES = (
    "normalize.py",
    "score.py",
    "confidence.py",
    "coverage.py",
    "tiers.py",
)
EVASION_DENYLIST = (
    "rotating_proxies",
    "proxy_rotation",
    "fingerprint_evasion",
    "undetected_chromedriver",
    "stealth_browser",
)


def lint_ack_after_commit(source: str, *, filename: str = "<source>") -> None:
    """Guard 1: ban raw `.xack(` outside the process_task template."""
    if PROCESS_TASK_TEMPLATE not in source and ".xack(" in source:
        raise GuardViolation(
            f"{filename}: raw `.xack(` is banned outside `{PROCESS_TASK_TEMPLATE}()`"
        )
    if PROCESS_TASK_TEMPLATE in source:
        tree = ast.parse(source, filename=filename)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "xack":
                if not _call_inside_process_task(tree, node):
                    raise GuardViolation(
                        f"{filename}: `.xack(` must appear only inside `{PROCESS_TASK_TEMPLATE}()`"
                    )


def _call_inside_process_task(tree: ast.Module, target: ast.Call) -> bool:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == PROCESS_TASK_TEMPLATE:
            for inner in ast.walk(node):
                if inner is target:
                    return True
    return False


def lint_outbox_xadd(source: str, *, path: Path) -> None:
    """Guard 4: ban XADD to cp:* streams outside dispatcher/."""
    if "dispatcher" in path.parts:
        return
    if XADD_PATTERN.search(source):
        raise GuardViolation(
            f"{path}: `XADD` to `cp:*` is banned outside dispatcher/"
        )


def lint_direct_artifact_insert(source: str, *, path: Path) -> None:
    """Guard 7: ban direct INSERT INTO artifacts outside persist_artifact wrapper."""
    if "persist_artifact" in path.name:
        return
    if ARTIFACT_INSERT_PATTERN.search(source):
        raise GuardViolation(
            f"{path}: direct INSERT INTO artifacts is banned; use persist_artifact()"
        )


def lint_import_purity(source: str, *, path: Path) -> None:
    """Guard 9: deterministic signal_engine modules must not import gateway/network."""
    if path.name not in DETERMINISTIC_MODULE_NAMES:
        return
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _reject_forbidden_import(alias.name, path)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                _reject_forbidden_import(full, path)


def lint_no_evasion_deps(source: str, *, path: Path) -> None:
    """Guard 10 static: denylisted proxy/fingerprint libraries."""
    lowered = source.lower()
    for token in EVASION_DENYLIST:
        if token in lowered:
            raise GuardViolation(
                f"{path}: evasion dependency `{token}` is denylisted (doc 06 §2.7)"
            )


def _reject_forbidden_import(module_name: str, path: Path) -> None:
    for prefix in FORBIDDEN_IMPORT_PREFIXES:
        if module_name == prefix or module_name.startswith(f"{prefix}."):
            raise GuardViolation(
                f"{path}: deterministic module must not import `{module_name}`"
            )


def scan_python_source(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    lint_ack_after_commit(source, filename=str(path))
    lint_outbox_xadd(source, path=path)
    lint_direct_artifact_insert(source, path=path)
    lint_import_purity(source, path=path)
    lint_no_evasion_deps(source, path=path)
