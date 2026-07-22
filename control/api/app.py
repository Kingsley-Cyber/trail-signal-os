"""FastAPI application factory for the control API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, status

from control.api.readiness import ReconcilerReadiness
from control.api.routes_dead_letters import router as dead_letters_router
from control.api.routes_domains import router as domains_router
from control.api.routes_jobs import router as jobs_router
from control.api.routes_lineage import router as lineage_router
from control.api.routes_workers import router as workers_router
from control.api.settings import (
    CONTROL_API_PORT,
    ControlApiSettings,
    load_control_api_settings,
)


def create_app(
    *,
    settings: ControlApiSettings | None = None,
    readiness: ReconcilerReadiness | None = None,
    run_startup_reconciler: bool = True,
) -> FastAPI:
    cfg = settings or load_control_api_settings()
    ready = readiness or ReconcilerReadiness()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_startup_reconciler:
            await ready.start_first_pass()
        yield

    app = FastAPI(title="trail-signal-os control API", lifespan=lifespan)
    app.state.settings = cfg
    app.state.readiness = ready

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        if not ready.is_ready:
            if ready.first_pass_error is not None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "status": "not_ready",
                        "reason": "reconciler_first_pass_failed",
                        "error": ready.first_pass_error,
                    },
                )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"status": "not_ready", "reason": "reconciler_first_pass_pending"},
            )
        return {"status": "ready", "reconciler_first_pass": True}

    app.include_router(jobs_router)
    app.include_router(workers_router)
    app.include_router(domains_router)
    app.include_router(dead_letters_router)
    app.include_router(lineage_router)

    return app
