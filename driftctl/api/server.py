"""
driftctl/api/server.py

FastAPI application factory.

Creates and configures the FastAPI app with:
  - Lifespan context (start/stop scheduler)
  - API key middleware (optional)
  - All route groups mounted
  - Static files served at GET /
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from driftctl.api.middleware import APIKeyMiddleware
from driftctl.api.routes import health, scans, workspaces

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Runs startup code before yielding, teardown after.
    """
    # Startup
    logger.info("driftctl server starting")
    try:
        from driftctl.scheduler.jobs import start_scheduler
        start_scheduler()
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)

    yield

    # Shutdown
    try:
        from driftctl.scheduler.jobs import stop_scheduler
        stop_scheduler()
    except Exception:
        pass
    logger.info("driftctl server stopped")


def create_app(api_key: str = "") -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        api_key: When non-empty, all API endpoints require
                 the X-API-Key header with this value.
    """
    application = FastAPI(
        title="driftctl",
        description="Terraform drift detector REST API",
        version="1.0.0",
        lifespan=_lifespan,
    )

    # CORS — permissive for the local dashboard
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Optional API key auth
    if api_key:
        application.add_middleware(APIKeyMiddleware, api_key=api_key)

    # Routes
    application.include_router(health.router)
    application.include_router(workspaces.router, prefix="/api/v1")
    application.include_router(scans.router, prefix="/api/v1")

    # Serve the web dashboard at GET /
    import os
    from pathlib import Path

    # Try multiple locations so it works locally and in Docker
    possible_dirs = [
        os.path.join(os.path.dirname(__file__), "static"),
        os.path.join(os.getcwd(), "driftctl", "api", "static"),
        "/app/driftctl/api/static",
    ]
    static_dir = None
    for d in possible_dirs:
        if os.path.isdir(d):
            static_dir = d
            break

    if static_dir:
        @application.get("/", include_in_schema=False)
        async def dashboard():
            index = os.path.join(static_dir, "index.html")
            if os.path.exists(index):
                return FileResponse(index)
            return {"message": f"Dashboard not found. Looked in: {static_dir}"}
    else:
        @application.get("/", include_in_schema=False)
        async def dashboard_missing():
            return {"message": "Dashboard static dir not found", "tried": possible_dirs}

    return application


# Module-level app instance (used by uvicorn)
app = create_app()
