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

    # Auto-create demo workspace pointing to the bundled sample tfstate
    try:
        _seed_demo_workspace()
    except Exception as exc:
        logger.warning("Could not seed demo workspace: %s", exc)

    yield

    # Shutdown
    try:
        from driftctl.scheduler.jobs import stop_scheduler
        stop_scheduler()
    except Exception:
        pass
    logger.info("driftctl server stopped")


def _seed_demo_workspace() -> None:
    """
    Create a demo workspace on first startup using the bundled sample.tfstate.
    This lets anyone visiting the deployed app click Scan Now immediately.
    Uses skip-cloud mode so no AWS credentials are needed for the demo.
    """
    import os
    from pathlib import Path
    from driftctl.storage.db import get_workspace_by_name, save_workspace

    # Already exists — don't recreate
    if get_workspace_by_name("demo"):
        return

    # Find the sample tfstate bundled with the app
    possible_paths = [
        Path(__file__).parent.parent.parent / "testdata" / "sample.tfstate",
        Path("/app/testdata/sample.tfstate"),
        Path("testdata/sample.tfstate"),
    ]
    state_path = None
    for p in possible_paths:
        if p.exists():
            state_path = str(p)
            break

    if not state_path:
        logger.warning("sample.tfstate not found, skipping demo workspace creation")
        return

    save_workspace(
        name="demo",
        state_path=state_path,
        region="us-east-1",
        state_backend="local",
        detect_unmanaged=False,
    )
    logger.info("Demo workspace created with state: %s", state_path)

    # Run an initial demo scan immediately (skip-cloud so no AWS needed)
    try:
        _run_demo_scan(state_path)
    except Exception as exc:
        logger.warning("Initial demo scan failed: %s", exc)


def _run_demo_scan(state_path: str) -> None:
    """Run a skip-cloud scan against the sample tfstate and save results."""
    import uuid
    from datetime import datetime, timezone
    from driftctl.engine.drift import detect_drift
    from driftctl.engine.remediate import enrich_results
    from driftctl.models import ScanReport
    from driftctl.state.extractor import extract_from_state
    from driftctl.state.reader import read_state
    from driftctl.storage.db import save_scan

    raw_records = read_state(state_path)
    expected = [
        r for r in (
            extract_from_state(rec["type"], rec["name"], rec["attributes"])
            for rec in raw_records
        )
        if r is not None
    ]

    # skip-cloud: actual = [] so all resources show as MISSING
    # This demonstrates the tool working without needing AWS credentials
    results = detect_drift(expected, [], detect_unmanaged=False)
    enrich_results(results)

    report = ScanReport(
        scan_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        state_path=state_path,
        region="us-east-1",
        workspace="demo",
        results=results,
    )
    save_scan(report)
    logger.info("Demo scan complete: %d results", len(results))


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
