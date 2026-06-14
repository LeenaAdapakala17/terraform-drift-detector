"""
driftctl/scheduler/jobs.py

APScheduler-based cron scheduler.

Runs inside the FastAPI server process as a background thread.
No separate worker, no Redis, no Celery — single container deployment.

On server start:
  - Reads all workspaces with a schedule_cron value from SQLite
  - Registers a cron job for each one
  - Jobs call the same scan pipeline as the REST API trigger

When a schedule is updated via the REST API:
  - The old job is removed
  - A new one is registered with the updated cron
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Module-level scheduler instance
_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    """Return the module-level scheduler, creating it if needed."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def start_scheduler() -> None:
    """
    Start the scheduler and register jobs for all workspaces
    that have a cron schedule configured.
    """
    scheduler = get_scheduler()
    if scheduler.running:
        return

    scheduler.start()
    logger.info("Scheduler started")

    # Register jobs for all scheduled workspaces
    try:
        from driftctl.storage.db import list_workspaces
        workspaces = list_workspaces()
        for ws in workspaces:
            if ws.get("schedule_cron"):
                register_job(ws["id"], ws["name"], ws["schedule_cron"])
    except Exception as exc:
        logger.warning("Could not load workspaces for scheduling: %s", exc)


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
    _scheduler = None


def register_job(
    workspace_id: str,
    workspace_name: str,
    cron_expr: str,
) -> None:
    """
    Register (or replace) a cron job for a workspace.

    Args:
        workspace_id:   UUID of the workspace
        workspace_name: Human-readable name (for logging)
        cron_expr:      5-field cron expression e.g. "0 6 * * *"
    """
    scheduler = get_scheduler()
    job_id = f"workspace_{workspace_id}"

    try:
        trigger = _parse_cron(cron_expr)
    except Exception as exc:
        logger.error(
            "Invalid cron expression '%s' for workspace %s: %s",
            cron_expr, workspace_name, exc,
        )
        return

    # Remove existing job if present
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        func=_run_scan_for_workspace,
        trigger=trigger,
        args=[workspace_id, workspace_name],
        id=job_id,
        name=f"drift-scan:{workspace_name}",
        replace_existing=True,
        misfire_grace_time=300,   # allow 5-minute misfires
    )
    logger.info(
        "Registered cron job for workspace '%s': %s",
        workspace_name, cron_expr,
    )


def remove_job(workspace_id: str) -> None:
    """Remove the cron job for a workspace (e.g. when workspace is deleted)."""
    scheduler = get_scheduler()
    job_id = f"workspace_{workspace_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("Removed cron job for workspace_id %s", workspace_id)


def list_jobs() -> list[dict]:
    """Return a list of currently registered jobs (for API/debug)."""
    scheduler = get_scheduler()
    jobs = []
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "job_id":   job.id,
            "name":     job.name,
            "next_run": next_run.isoformat() if next_run else None,
        })
    return jobs


# ---------------------------------------------------------------------------
# Job function — runs in the scheduler background thread
# ---------------------------------------------------------------------------

def _run_scan_for_workspace(
    workspace_id: str,
    workspace_name: str,
) -> None:
    """
    Execute a full drift scan for a workspace.
    Called by APScheduler on the configured cron schedule.
    """
    logger.info(
        "Scheduled scan starting for workspace '%s'", workspace_name
    )
    try:
        from driftctl.storage.db import get_workspace_by_name
        ws = get_workspace_by_name(workspace_name)
        if not ws:
            logger.error(
                "Scheduled scan: workspace '%s' not found", workspace_name
            )
            return

        _execute_scan(ws)

    except Exception as exc:
        logger.error(
            "Scheduled scan failed for workspace '%s': %s",
            workspace_name, exc,
        )


def _execute_scan(ws: dict) -> None:
    """Run the drift scan pipeline for a workspace dict from SQLite."""
    from driftctl.engine.drift import detect_drift
    from driftctl.engine.remediate import enrich_results
    from driftctl.models import ScanReport
    from driftctl.providers.registry import DefaultRegistry
    from driftctl.state.extractor import extract_from_state
    from driftctl.state.reader import read_state
    from driftctl.storage.db import save_scan

    state_path = ws.get("state_path", "")
    region = ws.get("region", "us-east-1")

    # Determine state source URI
    backend = ws.get("state_backend", "local")
    if backend == "s3":
        source = state_path  # already stored as s3:// URI
    else:
        source = state_path

    # Read state
    raw_records = read_state(source, region=region)
    expected = [
        r for r in (
            extract_from_state(rec["type"], rec["name"], rec["attributes"])
            for rec in raw_records
        )
        if r is not None
    ]

    # Fetch live resources
    provider = DefaultRegistry(region=region).get("aws")
    actual = []
    if provider:
        resource_types = list({r.type for r in expected})
        for rt in resource_types:
            actual.extend(provider.fetch(rt))

    # Detect drift + remediation
    results = detect_drift(
        expected, actual,
        detect_unmanaged=bool(ws.get("detect_unmanaged", False)),
    )
    enrich_results(results)

    report = ScanReport(
        scan_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        state_path=source,
        region=region,
        workspace=ws.get("name"),
        results=results,
    )
    save_scan(report)
    logger.info(
        "Scheduled scan complete for '%s': %d drifted",
        ws.get("name"), report.drifted_count,
    )


# ---------------------------------------------------------------------------
# Cron parser
# ---------------------------------------------------------------------------

def _parse_cron(expr: str) -> CronTrigger:
    """
    Parse a 5-field cron expression into an APScheduler CronTrigger.

    Examples:
      "0 6 * * *"   → daily at 06:00 UTC
      "0 */6 * * *" → every 6 hours
      "0 9 * * 1"   → every Monday at 09:00 UTC
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"Invalid cron expression '{expr}': expected 5 fields "
            f"(minute hour day_of_month month day_of_week), got {len(parts)}"
        )
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone="UTC",
    )
