"""Host memory metrics and pressure classification (environment_profile §2)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from control.resources.settings import load_phases_config


class PressureLevel(str, Enum):
    GREEN = "GREEN"
    ORANGE = "ORANGE"
    RED = "RED"


@dataclass(frozen=True)
class MemoryMetrics:
    vm_total_bytes: int
    vm_used_bytes: int
    vm_used_pct: float
    macos_pressure: str | None = None

    @property
    def vm_total_gib(self) -> float:
        return self.vm_total_bytes / (1024**3)


class MetricsProvider(Protocol):
    def read_memory_metrics(self) -> MemoryMetrics: ...


def _governor_thresholds() -> tuple[float, float, float]:
    config = load_phases_config().get("memory_governor", {})
    vm_total_gib = float(config.get("vm_total_gib", 25.4))
    green_below = float(config.get("green_below_pct", 70))
    orange_below = float(config.get("orange_below_pct", 85))
    return vm_total_gib, green_below, orange_below


def classify_pressure(
    metrics: MemoryMetrics,
    *,
    green_below_pct: float | None = None,
    orange_below_pct: float | None = None,
) -> PressureLevel:
    """Map VM utilization to GREEN / ORANGE / RED watermarks."""
    _, default_green, default_orange = _governor_thresholds()
    green = green_below_pct if green_below_pct is not None else default_green
    orange = orange_below_pct if orange_below_pct is not None else default_orange

    pct = metrics.vm_used_pct
    if pct >= orange:
        return PressureLevel.RED
    if pct >= green:
        return PressureLevel.ORANGE
    return PressureLevel.GREEN


def _read_macos_memory_pressure() -> str | None:
    try:
        completed = subprocess.run(
            ["memory_pressure"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    match = re.search(r"system-wide memory free percentage:\s*(\d+)%", completed.stdout)
    if match is None:
        return None
    free_pct = int(match.group(1))
    if free_pct >= 50:
        return "normal"
    if free_pct >= 25:
        return "warn"
    return "critical"


def _read_docker_vm_memory(vm_total_gib: float) -> MemoryMetrics | None:
    try:
        completed = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.MemUsage}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None

    total_used = 0
    total_limit = 0
    for line in completed.stdout.splitlines():
        usage = line.strip()
        if not usage or usage == "MEM USAGE":
            continue
        match = re.match(
            r"([\d.]+)([KMG]iB)\s*/\s*([\d.]+)([KMG]iB)",
            usage,
        )
        if match is None:
            continue
        used_val, used_unit, _limit_val, _limit_unit = match.groups()
        multiplier = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[used_unit]
        total_used += int(float(used_val) * multiplier)

    if total_used <= 0:
        return None

    vm_total_bytes = int(vm_total_gib * (1024**3))
    vm_used_pct = (total_used / vm_total_bytes) * 100.0
    return MemoryMetrics(
        vm_total_bytes=vm_total_bytes,
        vm_used_bytes=total_used,
        vm_used_pct=vm_used_pct,
        macos_pressure=_read_macos_memory_pressure(),
    )


def read_memory_metrics(*, provider: MetricsProvider | None = None) -> MemoryMetrics:
    """Read VM-wide memory utilization; falls back to conservative GREEN offline."""
    if provider is not None:
        return provider.read_memory_metrics()

    vm_total_gib, _, _ = _governor_thresholds()
    live = _read_docker_vm_memory(vm_total_gib)
    if live is not None:
        return live

    vm_total_bytes = int(vm_total_gib * (1024**3))
    return MemoryMetrics(
        vm_total_bytes=vm_total_bytes,
        vm_used_bytes=0,
        vm_used_pct=0.0,
        macos_pressure=_read_macos_memory_pressure(),
    )
