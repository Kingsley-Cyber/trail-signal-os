"""Lineage API placeholders — implemented by N17 lineage module."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/v1/lineage", tags=["lineage"])


@router.get("/trace/{artifact_id}")
def trace_artifact(artifact_id: str) -> dict[str, Any]:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "feature": "lineage.trace",
            "artifact_id": artifact_id,
            "message": "lineage trace is provided by the lineage node (N17)",
        },
    )


@router.get("/diff")
def diff_lineage() -> dict[str, Any]:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "feature": "lineage.diff",
            "message": "lineage diff is provided by the lineage node (N17)",
        },
    )


@router.post("/replay")
def replay_lineage() -> dict[str, Any]:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "feature": "lineage.replay",
            "message": "lineage replay is provided by the lineage node (N17)",
        },
    )
