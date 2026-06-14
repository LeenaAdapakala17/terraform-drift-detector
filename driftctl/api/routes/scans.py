"""
driftctl/api/routes/scans.py

Scan endpoints:
  GET /api/v1/scans              — list recent scans
  GET /api/v1/scans/{id}         — get scan metadata
  GET /api/v1/scans/{id}/report  — full drift report (?format=json|table)
  GET /api/v1/scans/{id}/summary — lightweight summary (dashboard polling)
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter(tags=["scans"])


def _ok(data) -> JSONResponse:
    return JSONResponse({"data": data, "error": None})


def _err(code: str, message: str, status: int = 404):
    from fastapi import HTTPException
    raise HTTPException(
        status_code=status,
        detail={"data": None, "error": {"code": code, "message": message}},
    )


@router.get("/scans")
async def list_scans(
    workspace: Optional[str] = Query(None, description="Filter by workspace name"),
    limit: int = Query(20, ge=1, le=200),
):
    """List recent scans, newest first."""
    from driftctl.storage.db import list_scans as db_list
    return _ok(db_list(workspace=workspace, limit=limit))


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: str):
    """Get scan metadata and status."""
    from driftctl.storage.db import _connect
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT s.*, w.name as workspace_name
            FROM scans s
            LEFT JOIN workspaces w ON s.workspace_id = w.id
            WHERE s.id = ?
            """,
            (scan_id,),
        ).fetchone()

    if not row:
        _err("NOT_FOUND", f"Scan '{scan_id}' not found")

    return _ok(dict(row))


@router.get("/scans/{scan_id}/report")
async def get_scan_report(
    scan_id: str,
    format: str = Query("json", description="json or table"),
):
    """Get the full drift report for a completed scan."""
    from driftctl.storage.db import get_scan
    report = get_scan(scan_id)
    if not report:
        _err("NOT_FOUND", f"Scan '{scan_id}' not found")

    if format == "table":
        from driftctl.report.table_renderer import render_table_string
        text = render_table_string(report)
        return PlainTextResponse(text)

    from driftctl.report.json_renderer import render_json
    return _ok(render_json(report, verbose=True))


@router.get("/scans/{scan_id}/summary")
async def get_scan_summary(scan_id: str):
    """
    Lightweight summary for dashboard polling.
    Returns status + counts without the full results list.
    """
    from driftctl.storage.db import _connect
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, status, drifted_count, total_resources, exit_code "
            "FROM scans WHERE id = ?",
            (scan_id,),
        ).fetchone()

    if not row:
        _err("NOT_FOUND", f"Scan '{scan_id}' not found")

    return _ok({
        "scan_id":         row["id"],
        "status":          row["status"],
        "drifted_count":   row["drifted_count"],
        "total_resources": row["total_resources"],
        "exit_code":       row["exit_code"],
    })
