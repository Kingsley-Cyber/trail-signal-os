"""Domain profile routes (Gate 1 placeholder until routing profiles land)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/v1/domains", tags=["domains"])


@router.get("/{domain}")
def get_domain_profile(domain: str) -> dict[str, Any]:
    if not domain or domain.strip() != domain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid domain",
        )
    return {
        "domain": domain,
        "status": "unknown",
        "profile_loaded": False,
        "message": "domain profiles are not persisted until routing node is built",
    }
