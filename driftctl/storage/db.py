"""
driftctl/storage/db.py

Persistence layer — supports two backends:

1. SQLite (default) — local file, stdlib sqlite3
   Used when TURSO_DATABASE_URL is NOT set.
   Database file: driftctl.db (configurable via DRIFTCTL_DB env var).

2. Turso (cloud SQLite) — used when TURSO_DATABASE_URL + TURSO_AUTH_TOKEN
   are set as environment variables.
   Free tier: 500MB, survives Render restarts.
   Install: pip install libsql-client

Schema is identical for both backends.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from driftctl.models import DriftResult, ScanReport

logger = logging.getLogger(__name__)

# Default database path — can be overridden via DRIFTCTL_DB env var
_DEFAULT_DB = "driftctl.db"
_db_path: str = _DEFAULT_DB

# Turso connection cache
_turso_client = None


def set_db_path(path: str) -> None:
    """Override the database file path (called by config loader)."""
    global _db_path
    _db_path = path


def get_db_path() -> str:
    return _db_path


def _is_turso() -> bool:
    """Return True when Turso env vars are configured."""
    return bool(
        os.environ.get("TURSO_DATABASE_URL") and
        os.environ.get("TURSO_AUTH_TOKEN")
    )


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """
    Open a database connection.
    Uses Turso when env vars are set, otherwise local SQLite.
    """
    if _is_turso():
        return _connect_turso()
    return _connect_sqlite()


def _connect_sqlite() -> sqlite3.Connection:
    """Open a local SQLite connection."""
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _connect_turso() -> sqlite3.Connection:
    """
    Open a Turso (cloud SQLite) connection via libsql-client.
    Falls back to local SQLite if libsql-client is not installed.
    """
    try:
        import libsql_client
    except ImportError:
        logger.warning(
            "libsql-client not installed, falling back to local SQLite. "
            "Install with: pip install libsql-client"
        )
        return _connect_sqlite()

    url   = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]

    conn = libsql_client.connect(url=url, auth_token=token)
    conn.row_factory = _turso_row_factory
    _ensure_schema(conn)
    logger.info("Connected to Turso database: %s", url)
    return conn


def _turso_row_factory(cursor, row):
    """Make Turso rows behave like sqlite3.Row (subscriptable by name)."""
    if hasattr(cursor, "description") and cursor.description:
        return dict(zip([d[0] for d in cursor.description], row))
    return row


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they don't exist yet."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL UNIQUE,
            provider          TEXT NOT NULL DEFAULT 'aws',
            state_backend     TEXT NOT NULL DEFAULT 'local',
            state_path        TEXT NOT NULL,
            state_region      TEXT,
            region            TEXT NOT NULL,
            detect_unmanaged  INTEGER NOT NULL DEFAULT 0,
            schedule_cron     TEXT,
            created_at        TEXT NOT NULL,
            last_scan_id      TEXT
        );

        CREATE TABLE IF NOT EXISTS scans (
            id                TEXT PRIMARY KEY,
            workspace_id      TEXT,
            created_at        TEXT NOT NULL,
            state_path        TEXT NOT NULL,
            region            TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'complete',
            drifted_count     INTEGER,
            total_resources   INTEGER,
            exit_code         INTEGER,
            error_message     TEXT,
            summary_json      TEXT,
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
        );

        CREATE TABLE IF NOT EXISTS drift_results (
            id                TEXT PRIMARY KEY,
            scan_id           TEXT NOT NULL,
            type              TEXT NOT NULL,
            resource_id       TEXT NOT NULL,
            resource_name     TEXT,
            status            TEXT NOT NULL,
            attribute_diffs   TEXT NOT NULL,
            tag_diffs         TEXT NOT NULL,
            remediation       TEXT,
            FOREIGN KEY (scan_id) REFERENCES scans(id)
        );

        CREATE INDEX IF NOT EXISTS idx_scans_workspace
            ON scans(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_scans_created
            ON scans(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_results_scan
            ON drift_results(scan_id);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------

def save_scan(report: ScanReport) -> None:
    """
    Persist a ScanReport to SQLite.
    Saves the scan header row and all drift_results rows.
    """
    with _connect() as conn:
        # Resolve workspace_id if report has a workspace name
        workspace_id = None
        if report.workspace:
            row = conn.execute(
                "SELECT id FROM workspaces WHERE name = ?",
                (report.workspace,),
            ).fetchone()
            if row:
                workspace_id = row["id"]

        summary = report.summary()

        conn.execute(
            """
            INSERT OR REPLACE INTO scans
              (id, workspace_id, created_at, state_path, region,
               status, drifted_count, total_resources, exit_code, summary_json)
            VALUES (?, ?, ?, ?, ?, 'complete', ?, ?, ?, ?)
            """,
            (
                report.scan_id,
                workspace_id,
                report.created_at,
                report.state_path,
                report.region,
                summary["drifted"],
                summary["total_resources"],
                report.exit_code,
                json.dumps(summary),
            ),
        )

        # Save individual drift results
        for result in report.results:
            conn.execute(
                """
                INSERT OR REPLACE INTO drift_results
                  (id, scan_id, type, resource_id, resource_name,
                   status, attribute_diffs, tag_diffs, remediation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    report.scan_id,
                    result.type,
                    result.id,
                    result.name,
                    result.status,
                    json.dumps(_serialise_diffs(result.attribute_diffs)),
                    json.dumps(result.tag_diffs),
                    result.remediation,
                ),
            )

        # Update workspace last_scan_id
        if workspace_id:
            conn.execute(
                "UPDATE workspaces SET last_scan_id = ? WHERE id = ?",
                (report.scan_id, workspace_id),
            )

        conn.commit()
    logger.info("Saved scan %s to %s", report.scan_id, _db_path)


def get_scan(scan_id: str) -> ScanReport | None:
    """
    Load a ScanReport from SQLite by scan_id.
    Returns None if not found.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        if not row:
            return None

        result_rows = conn.execute(
            "SELECT * FROM drift_results WHERE scan_id = ? ORDER BY rowid",
            (scan_id,),
        ).fetchall()

        # Resolve workspace name
        workspace_name = None
        if row["workspace_id"]:
            ws_row = conn.execute(
                "SELECT name FROM workspaces WHERE id = ?",
                (row["workspace_id"],),
            ).fetchone()
            if ws_row:
                workspace_name = ws_row["name"]

    results = [_row_to_drift_result(r) for r in result_rows]

    return ScanReport(
        scan_id=row["id"],
        created_at=row["created_at"],
        state_path=row["state_path"],
        region=row["region"],
        workspace=workspace_name,
        results=results,
    )


def list_scans(
    workspace: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    List recent scans, newest first.
    Optionally filter by workspace name.
    """
    with _connect() as conn:
        if workspace:
            ws_row = conn.execute(
                "SELECT id FROM workspaces WHERE name = ?", (workspace,)
            ).fetchone()
            ws_id = ws_row["id"] if ws_row else None
            rows = conn.execute(
                """
                SELECT s.id, s.created_at, s.state_path, s.region,
                       s.drifted_count, s.total_resources, s.exit_code,
                       w.name as workspace_name
                FROM scans s
                LEFT JOIN workspaces w ON s.workspace_id = w.id
                WHERE s.workspace_id = ?
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (ws_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT s.id, s.created_at, s.state_path, s.region,
                       s.drifted_count, s.total_resources, s.exit_code,
                       w.name as workspace_name
                FROM scans s
                LEFT JOIN workspaces w ON s.workspace_id = w.id
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    return [
        {
            "scan_id":         r["id"],
            "created_at":      r["created_at"],
            "state_path":      r["state_path"],
            "region":          r["region"],
            "workspace":       r["workspace_name"],
            "drifted_count":   r["drifted_count"],
            "total_resources": r["total_resources"],
            "exit_code":       r["exit_code"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

def save_workspace(
    name: str,
    state_path: str,
    region: str,
    state_backend: str = "local",
    state_region: str | None = None,
    detect_unmanaged: bool = False,
    schedule_cron: str | None = None,
    provider: str = "aws",
) -> str:
    """
    Insert or update a workspace. Returns the workspace id.
    """
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM workspaces WHERE name = ?", (name,)
        ).fetchone()

        if existing:
            ws_id = existing["id"]
            conn.execute(
                """
                UPDATE workspaces
                SET state_path = ?, region = ?, state_backend = ?,
                    state_region = ?, detect_unmanaged = ?,
                    schedule_cron = ?, provider = ?
                WHERE id = ?
                """,
                (
                    state_path, region, state_backend,
                    state_region, int(detect_unmanaged),
                    schedule_cron, provider, ws_id,
                ),
            )
        else:
            ws_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO workspaces
                  (id, name, provider, state_backend, state_path,
                   state_region, region, detect_unmanaged,
                   schedule_cron, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ws_id, name, provider, state_backend, state_path,
                    state_region, region, int(detect_unmanaged),
                    schedule_cron,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.commit()
    return ws_id


def list_workspaces() -> list[dict]:
    """Return all workspaces as a list of dicts."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM workspaces ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_workspace_by_name(name: str) -> dict | None:
    """Return a workspace dict by name, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workspaces WHERE name = ?", (name,)
        ).fetchone()
    return dict(row) if row else None


def update_schedule(workspace_name: str, cron: str) -> bool:
    """
    Update the cron schedule for a workspace.
    Returns True if the workspace was found and updated.
    """
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE workspaces SET schedule_cron = ? WHERE name = ?",
            (cron, workspace_name),
        )
        conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _row_to_drift_result(row: sqlite3.Row) -> DriftResult:
    """Reconstruct a DriftResult from a drift_results table row."""
    try:
        attr_diffs = json.loads(row["attribute_diffs"] or "{}")
    except json.JSONDecodeError:
        attr_diffs = {}

    try:
        tag_diffs = json.loads(row["tag_diffs"] or "{}")
    except json.JSONDecodeError:
        tag_diffs = {}

    return DriftResult(
        type=row["type"],
        id=row["resource_id"],
        name=row["resource_name"],
        status=row["status"],
        attribute_diffs=attr_diffs,
        tag_diffs=tag_diffs,
        remediation=row["remediation"],
    )


def _serialise_diffs(diffs: dict) -> dict:
    """
    Convert attribute_diffs to a JSON-safe dict.
    SGRule dataclasses and similar non-JSON types are converted to strings.
    """
    result = {}
    for field, diff in diffs.items():
        result[field] = {
            "expected": _to_json_safe(diff.get("expected")),
            "actual":   _to_json_safe(diff.get("actual")),
        }
    return result


def _to_json_safe(value: object) -> object:
    """Recursively make a value JSON-serialisable."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {f: _to_json_safe(getattr(value, f))
                for f in value.__dataclass_fields__}
    return str(value)
