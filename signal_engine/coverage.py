"""Deterministic niche coverage grid + COVERAGE_GATE rule (N25, LAW 1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

CODE_VERSION = "coverage-1.0.0"
COVERAGE_VERSION = CODE_VERSION

MIN_CELL_CONFIDENCE = 0.40
SIGNAL_TYPES = ("demand", "growth", "pain", "competition", "content")
SOURCE_TIERS = ("open", "defended", "hostile")
HARD_REQUIRED_SIGNAL_TYPES = ("demand", "pain")
HARD_REQUIRED_TIERS = ("open", "defended")
SOFT_SIGNAL_TYPES = ("growth", "competition", "content")


class CoverageError(Exception):
    """Deterministic coverage evaluation failed."""


def _parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@dataclass(frozen=True)
class CoverageCell:
    signal_type: str
    source_tier: str
    best_confidence: float
    contributing_signal_ids: tuple[str, ...]
    updated_at: str


@dataclass(frozen=True)
class CoverageGap:
    signal_type: str
    source_tier: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "signal_type": self.signal_type,
            "source_tier": self.source_tier,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CoverageGateResult:
    admitted: bool
    scores_with_gaps: bool
    coverage_gaps: tuple[dict[str, str], ...]
    hard_required_met: bool
    deadline_reached: bool


class CoverageGrid:
    """In-memory signal_type × source_tier grid of best confidences seen."""

    def __init__(self, *, niche_id: str) -> None:
        self.niche_id = niche_id
        self._cells: dict[tuple[str, str], CoverageCell] = {}

    def upsert_signal(
        self,
        signal: Mapping[str, Any],
        *,
        updated_at: str,
    ) -> bool:
        """Idempotent upsert; returns True when the stored cell changes."""
        signal_type = str(signal["signal_type"])
        source = signal.get("source")
        if not isinstance(source, Mapping):
            raise CoverageError("signal.source must be an object")
        source_tier = str(source["tier"])
        if signal_type not in SIGNAL_TYPES:
            raise CoverageError(f"unsupported signal_type: {signal_type!r}")
        if source_tier not in SOURCE_TIERS:
            raise CoverageError(f"unsupported source tier: {source_tier!r}")

        confidence = float(signal["confidence"])
        signal_id = str(signal["signal_id"])
        key = (signal_type, source_tier)
        existing = self._cells.get(key)
        if existing is None:
            self._cells[key] = CoverageCell(
                signal_type=signal_type,
                source_tier=source_tier,
                best_confidence=confidence,
                contributing_signal_ids=(signal_id,),
                updated_at=updated_at,
            )
            return True

        if confidence > existing.best_confidence:
            self._cells[key] = CoverageCell(
                signal_type=signal_type,
                source_tier=source_tier,
                best_confidence=confidence,
                contributing_signal_ids=(signal_id,),
                updated_at=updated_at,
            )
            return True

        if confidence == existing.best_confidence and signal_id not in existing.contributing_signal_ids:
            merged_ids = tuple(sorted((*existing.contributing_signal_ids, signal_id)))
            self._cells[key] = CoverageCell(
                signal_type=signal_type,
                source_tier=source_tier,
                best_confidence=confidence,
                contributing_signal_ids=merged_ids,
                updated_at=updated_at,
            )
            return True

        return False

    def get_cell(self, signal_type: str, source_tier: str) -> CoverageCell | None:
        return self._cells.get((signal_type, source_tier))

    def best_confidence(self, signal_type: str, source_tier: str) -> float:
        cell = self._cells.get((signal_type, source_tier))
        return cell.best_confidence if cell is not None else 0.0

    def best_across_tiers(self, signal_type: str, tiers: Sequence[str]) -> float:
        return max((self.best_confidence(signal_type, tier) for tier in tiers), default=0.0)

    def cells(self) -> tuple[CoverageCell, ...]:
        return tuple(
            self._cells[key]
            for key in sorted(self._cells)
        )


def build_grid_from_signals(
    *,
    niche_id: str,
    signals: Sequence[Mapping[str, Any]],
    updated_at: str,
) -> CoverageGrid:
    grid = CoverageGrid(niche_id=niche_id)
    for signal in signals:
        if str(signal.get("niche_id", niche_id)) != niche_id:
            raise CoverageError(
                f"signal niche_id {signal.get('niche_id')!r} does not match grid {niche_id!r}",
            )
        grid.upsert_signal(signal, updated_at=updated_at)
    return grid


def hard_required_met(
    grid: CoverageGrid,
    *,
    min_cell_confidence: float = MIN_CELL_CONFIDENCE,
) -> bool:
    for signal_type in HARD_REQUIRED_SIGNAL_TYPES:
        best = grid.best_across_tiers(signal_type, HARD_REQUIRED_TIERS)
        if best < min_cell_confidence:
            return False
    return True


def find_coverage_gaps(
    grid: CoverageGrid,
    *,
    min_cell_confidence: float = MIN_CELL_CONFIDENCE,
) -> tuple[CoverageGap, ...]:
    gaps: list[CoverageGap] = []

    for signal_type in HARD_REQUIRED_SIGNAL_TYPES:
        best = grid.best_across_tiers(signal_type, HARD_REQUIRED_TIERS)
        if best < min_cell_confidence:
            gaps.append(
                CoverageGap(
                    signal_type=signal_type,
                    source_tier="open",
                    reason=(
                        f"hard-required: best open|defended confidence "
                        f"{best:.3f} < {min_cell_confidence:.2f}"
                    ),
                ),
            )

    for signal_type in SOFT_SIGNAL_TYPES:
        best = grid.best_across_tiers(signal_type, SOURCE_TIERS)
        if best < min_cell_confidence:
            gaps.append(
                CoverageGap(
                    signal_type=signal_type,
                    source_tier="open",
                    reason=(
                        f"soft-required: best any-tier confidence "
                        f"{best:.3f} < {min_cell_confidence:.2f}"
                    ),
                ),
            )

    return tuple(gaps)


def evaluate_coverage_gate(
    grid: CoverageGrid,
    *,
    as_of: str,
    dossier_deadline_at: str | None = None,
    min_cell_confidence: float = MIN_CELL_CONFIDENCE,
) -> CoverageGateResult:
    """Doc 08 §6: admit when fully covered; else score-with-gaps if hard cells met."""
    _parse_timestamp(as_of)
    deadline_reached = False
    if dossier_deadline_at is not None:
        deadline_reached = _parse_timestamp(as_of) >= _parse_timestamp(dossier_deadline_at)

    hard_met = hard_required_met(grid, min_cell_confidence=min_cell_confidence)
    gaps = find_coverage_gaps(grid, min_cell_confidence=min_cell_confidence)
    soft_gaps = tuple(
        gap.to_dict()
        for gap in gaps
        if gap.signal_type in SOFT_SIGNAL_TYPES
    )
    all_gap_dicts = tuple(gap.to_dict() for gap in gaps)

    if hard_met and not gaps:
        return CoverageGateResult(
            admitted=True,
            scores_with_gaps=False,
            coverage_gaps=(),
            hard_required_met=True,
            deadline_reached=deadline_reached,
        )

    if hard_met and soft_gaps:
        return CoverageGateResult(
            admitted=False,
            scores_with_gaps=True,
            coverage_gaps=soft_gaps,
            hard_required_met=True,
            deadline_reached=deadline_reached,
        )

    if not hard_met and deadline_reached:
        return CoverageGateResult(
            admitted=False,
            scores_with_gaps=False,
            coverage_gaps=all_gap_dicts,
            hard_required_met=False,
            deadline_reached=True,
        )

    return CoverageGateResult(
        admitted=False,
        scores_with_gaps=False,
        coverage_gaps=(),
        hard_required_met=hard_met,
        deadline_reached=deadline_reached,
    )


__all__ = [
    "CODE_VERSION",
    "COVERAGE_VERSION",
    "CoverageCell",
    "CoverageError",
    "CoverageGap",
    "CoverageGateResult",
    "CoverageGrid",
    "HARD_REQUIRED_SIGNAL_TYPES",
    "HARD_REQUIRED_TIERS",
    "MIN_CELL_CONFIDENCE",
    "SIGNAL_TYPES",
    "SOFT_SIGNAL_TYPES",
    "SOURCE_TIERS",
    "build_grid_from_signals",
    "evaluate_coverage_gate",
    "find_coverage_gaps",
    "hard_required_met",
]
