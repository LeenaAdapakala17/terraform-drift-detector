"""
driftctl/api/routes/workspaces.py

Workspace endpoints:
  GET  /api/v1/workspaces           — list all
  POST /api/v1/workspaces           — create
  GET  /api/v1/workspaces/{id}      — get one
  DELETE /api/v1/workspaces/{id}    — delete
  POST /api/v1/workspaces/{id}/scans — trigger scan
  PUT  /api/v1/workspaces/{id}/schedules — update cron
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(tags=["workspaces"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class WorkspaceCreate(BaseModel):
    name: str
    provider: str = "aws"
    state_backend: str = "local"
    state_path: str
    state_region: Optional[str] = None
    region: str = "us-east-1"
    detect_unmanaged: bool = False
    schedule_cron: Optional[str] = None


class ScheduleUpdate(BaseModel):
    cron: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(data) -> JSONResponse:
    return JSONResponse({"data": data, "error": None})


def _err(code: str, message: str, status: int = 400) -> HTTPException:
    raise HTTPException(
        status_code=status,
        detail={"data": None, "error": {"code": code, "message": message}},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/workspaces")
async def list_workspaces():
    """List all workspaces."""
    from driftctl.storage.db import list_workspaces as db_list
    return _ok(db_list())


@router.post("/workspaces", status_code=201)
async def create_workspace(body: WorkspaceCreate):
    """Create a new workspace."""
    from driftctl.storage.db import get_workspace_by_name, save_workspace
    if get_workspace_by_name(body.name):
        _err("CONFLICT", f"Workspace '{body.name}' already exists", 409)

    ws_id = save_workspace(
        name=body.name,
        provider=body.provider,
        state_path=body.state_path,
        state_backend=body.state_backend,
        state_region=body.state_region,
        region=body.region,
        detect_unmanaged=body.detect_unmanaged,
        schedule_cron=body.schedule_cron,
    )

    # Register scheduler job if cron provided
    if body.schedule_cron:
        try:
            from driftctl.scheduler.jobs import register_job
            register_job(ws_id, body.name, body.schedule_cron)
        except Exception:
            pass

    from driftctl.storage.db import get_workspace_by_name
    return JSONResponse(
        {"data": get_workspace_by_name(body.name), "error": None},
        status_code=201,
    )


@router.get("/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str):
    """Get a workspace by id."""
    from driftctl.storage.db import list_workspaces
    all_ws = list_workspaces()
    ws = next((w for w in all_ws if w["id"] == workspace_id), None)
    if not ws:
        _err("NOT_FOUND", f"Workspace '{workspace_id}' not found", 404)
    return _ok(ws)


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: str):
    """Delete a workspace and its schedule."""
    import sqlite3
    from driftctl.storage.db import _connect, get_db_path
    from driftctl.scheduler.jobs import remove_job

    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM workspaces WHERE id = ?", (workspace_id,)
        )
        conn.commit()
        if cursor.rowcount == 0:
            _err("NOT_FOUND", f"Workspace '{workspace_id}' not found", 404)

    try:
        remove_job(workspace_id)
    except Exception:
        pass


@router.post("/workspaces/{workspace_id}/scans", status_code=202)
async def trigger_scan(
    workspace_id: str,
    background_tasks: BackgroundTasks,
):
    """Trigger an on-demand scan for a workspace."""
    from driftctl.storage.db import list_workspaces
    all_ws = list_workspaces()
    ws = next((w for w in all_ws if w["id"] == workspace_id), None)
    if not ws:
        _err("NOT_FOUND", f"Workspace '{workspace_id}' not found", 404)

    scan_id = str(uuid.uuid4())

    # Mark scan as pending in DB then run in background
    _create_pending_scan(scan_id, workspace_id, ws)
    background_tasks.add_task(_run_scan_background, ws, scan_id)

    return JSONResponse(
        {"data": {"scan_id": scan_id, "status": "running"}, "error": None},
        status_code=202,
    )


@router.put("/workspaces/{workspace_id}/schedules")
async def update_schedule(workspace_id: str, body: ScheduleUpdate):
    """Set or update the cron schedule for a workspace."""
    from driftctl.storage.db import list_workspaces, update_schedule as db_update
    all_ws = list_workspaces()
    ws = next((w for w in all_ws if w["id"] == workspace_id), None)
    if not ws:
        _err("NOT_FOUND", f"Workspace '{workspace_id}' not found", 404)

    db_update(ws["name"], body.cron)

    try:
        from driftctl.scheduler.jobs import register_job
        register_job(workspace_id, ws["name"], body.cron)
    except Exception:
        pass

    return _ok({"workspace_id": workspace_id, "cron": body.cron})


# ---------------------------------------------------------------------------
# Background scan helpers
# ---------------------------------------------------------------------------

def _create_pending_scan(
    scan_id: str,
    workspace_id: str,
    ws: dict,
) -> None:
    """Insert a 'running' scan row so the GET endpoint can poll it."""
    import sqlite3
    from driftctl.storage.db import _connect
    from datetime import datetime, timezone
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO scans
              (id, workspace_id, created_at, state_path, region, status)
            VALUES (?, ?, ?, ?, ?, 'running')
            """,
            (
                scan_id,
                workspace_id,
                datetime.now(timezone.utc).isoformat(),
                ws.get("state_path", ""),
                ws.get("region", "us-east-1"),
            ),
        )
        conn.commit()


def _run_scan_background(ws: dict, scan_id: str) -> None:
    """Run the full scan pipeline and update the scan row when done."""
    from driftctl.scheduler.jobs import _execute_scan
    from driftctl.storage.db import _connect
    try:
        # Run scan — _execute_scan saves its own scan row;
        # update our placeholder row with the result
        _execute_scan(ws)
        status = "complete"
        error_msg = None
    except Exception as exc:
        status = "error"
        error_msg = str(exc)

    with _connect() as conn:
        conn.execute(
            "UPDATE scans SET status = ?, error_message = ? WHERE id = ?",
            (status, error_msg, scan_id),
        )
        conn.commit()
