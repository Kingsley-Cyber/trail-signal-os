"""Lineage trace / diff / replay HTTP routes (N17)."""

from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from control.api.deps import get_db
from lineage.diff import diff_lineage
from lineage.edges import list_edges
from lineage.replay import replay_lineage
from lineage.trace import resolve_root, trace

router = APIRouter(prefix="/v1/lineage", tags=["lineage"])


class ReplayRequest(BaseModel):
    artifact_id: str = Field(min_length=1)
    kind: str | None = None
    pin_versions: bool = True


@router.get("/edges")
def get_lineage_edges(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    child_kind: str | None = None,
    child_id: str | None = None,
    parent_kind: str | None = None,
    parent_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    edges = list_edges(
        conn,
        child_kind=child_kind,
        child_id=child_id,
        parent_kind=parent_kind,
        parent_id=parent_id,
        limit=limit,
    )
    return {"edges": [edge.as_dict() for edge in edges], "count": len(edges)}


@router.get("/trace/{artifact_id}")
def trace_artifact(
    artifact_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    kind: str | None = None,
) -> dict[str, Any]:
    try:
        result = trace(conn, artifact_id, kind=kind)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return result.as_dict()


@router.get("/diff")
def diff_artifacts(
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    left_id: str = Query(min_length=1),
    right_id: str = Query(min_length=1),
    left_kind: str | None = None,
    right_kind: str | None = None,
) -> dict[str, Any]:
    try:
        lk, lid = resolve_root(conn, left_id, kind=left_kind)
        rk, rid = resolve_root(conn, right_id, kind=right_kind)
        result = diff_lineage(
            conn,
            left_kind=lk,
            left_id=lid,
            right_kind=rk,
            right_id=rid,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return result.as_dict()


@router.post("/replay")
def replay_artifact(
    body: ReplayRequest,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
) -> dict[str, Any]:
    try:
        plan = replay_lineage(
            conn,
            body.artifact_id,
            kind=body.kind,
            pin_versions=body.pin_versions,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return plan.as_dict()
