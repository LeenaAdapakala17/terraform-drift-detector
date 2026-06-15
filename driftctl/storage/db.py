"""
driftctl/storage/db.py

Persistence layer — supports two backends:

1. PostgreSQL (cloud) — used when DATABASE_URL env var is set.
   Free tier on Render. Persists forever across restarts and redeploys.
   Requires: psycopg2-binary

2. SQLite (default) — local file, stdlib sqlite3.
   Used when DATABASE_URL is NOT set.
   Database file configured via DRIFTCTL_DB env var (default: driftctl.db).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from driftctl.models import DriftResult, ScanReport

logger = logging.getLogger(__name__)

_DEFAULT_DB = "driftctl.db"
_db_path: str = _DEFAULT_DB


def set_db_path(path: str) -> None:
    global _db_path
    _db_path = path


def get_db_path() -> str:
    return _db_path


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _is_postgres() -> bool:
    return bool(_database_url())


# ---------------------------------------------------------------------------
# Connection context manager
# ---------------------------------------------------------------------------

class _Connection:
    """
    Unified connection wrapper for SQLite and PostgreSQL.
    Used as: with _connect() as conn: ...
    Supports execute(), fetchall(), fetchone(), commit().
    """

    def __init__(self, pg_conn=None, sq_conn=None):
        self._pg = pg_conn
        self._sq = sq_conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        try:
            if exc_type is None:
                self.commit()
        finally:
            self.close()

    def execute(self, sql: str, params: tuple = ()):
        if self._pg:
            import psycopg2.extras
            cur = self._pg.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            )
            cur.execute(sql.replace("?", "%s"), params)
            return _PgCursor(cur)
        else:
            return self._sq.execute(sql, params)

    def executescript(self, sql: str):
        if self._sq:
            self._sq.executescript(sql)

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        if self._pg:
            import psycopg2.extras
            cur = self._pg.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            )
            cur.execute(sql.replace("?", "%s"), params)
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]
        else:
            rows = self._sq.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def commit(self):
        if self._pg:
            self._pg.commit()
        elif self._sq:
            self._sq.commit()

    def close(self):
        try:
            if self._pg:
                self._pg.close()
            elif self._sq:
                self._sq.close()
        except Exception:
            pass


class _PgCursor:
    """Minimal cursor wrapper so execute() return value isn't used."""
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


def _connect() -> _Connection:
    """Open a database connection (PostgreSQL or SQLite)."""
    if _is_postgres():
        return _pg_connect()
    return _sqlite_connect()


def _sqlite_connect() -> _Connection:
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    c = _Connection(sq_conn=conn)
    _ensure_schema(c)
    return c


def _pg_connect() -> _Connection:
    try:
        import psycopg2
    except ImportError:
        logger.error("psycopg2 not installed. Run: pip install psycopg2-binary")
        raise
    url = _database_url()
    pg = psycopg2.connect(url)
    c = _Connection(pg_conn=pg)
    _ensure_schema(c)
    return c


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _ensure_schema(conn: _Connection) -> None:
    if _is_postgres():
        _ensure_schema_pg(conn)
    else:
        _ensure_schema_sqlite(conn)


def _ensure_schema_sqlite(conn: _Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL DEFAULT 'aws',
            state_backend TEXT NOT NULL DEFAULT 'local',
            state_path TEXT NOT NULL, state_region TEXT,
            region TEXT NOT NULL, detect_unmanaged INTEGER NOT NULL DEFAULT 0,
            schedule_cron TEXT, created_at TEXT NOT NULL, last_scan_id TEXT
        );
        CREATE TABLE IF NOT EXISTS scans (
            id TEXT PRIMARY KEY, workspace_id TEXT, created_at TEXT NOT NULL,
            state_path TEXT NOT NULL, region TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'complete',
            drifted_count INTEGER, total_resources INTEGER,
            exit_code INTEGER, error_message TEXT, summary_json TEXT
        );
        CREATE TABLE IF NOT EXISTS drift_results (
            id TEXT PRIMARY KEY, scan_id TEXT NOT NULL,
            type TEXT NOT NULL, resource_id TEXT NOT NULL,
            resource_name TEXT, status TEXT NOT NULL,
            attribute_diffs TEXT NOT NULL, tag_diffs TEXT NOT NULL,
            remediation TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_scans_workspace ON scans(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_results_scan ON drift_results(scan_id);
    """)
    conn.commit()


def _ensure_schema_pg(conn: _Connection) -> None:
    stmts = [
        """CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL DEFAULT 'aws',
            state_backend TEXT NOT NULL DEFAULT 'local',
            state_path TEXT NOT NULL, state_region TEXT,
            region TEXT NOT NULL, detect_unmanaged INTEGER NOT NULL DEFAULT 0,
            schedule_cron TEXT, created_at TEXT NOT NULL, last_scan_id TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS scans (
            id TEXT PRIMARY KEY, workspace_id TEXT, created_at TEXT NOT NULL,
            state_path TEXT NOT NULL, region TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'complete',
            drifted_count INTEGER, total_resources INTEGER,
            exit_code INTEGER, error_message TEXT, summary_json TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS drift_results (
            id TEXT PRIMARY KEY, scan_id TEXT NOT NULL,
            type TEXT NOT NULL, resource_id TEXT NOT NULL,
            resource_name TEXT, status TEXT NOT NULL,
            attribute_diffs TEXT NOT NULL, tag_diffs TEXT NOT NULL,
            remediation TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_scans_workspace ON scans(workspace_id)",
        "CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_results_scan ON drift_results(scan_id)",
    ]
    for stmt in stmts:
        conn.execute(stmt)
    conn.commit()


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------

def save_scan(report: ScanReport) -> None:
    """Persist a ScanReport to the database."""
    with _connect() as conn:
        workspace_id = None
        if report.workspace:
            row = conn.fetchone(
                "SELECT id FROM workspaces WHERE name = ?",
                (report.workspace,))
            if row:
                workspace_id = row["id"]

        summary = report.summary()

        if _is_postgres():
            conn.execute("""
                INSERT INTO scans
                  (id, workspace_id, created_at, state_path, region,
                   status, drifted_count, total_resources, exit_code, summary_json)
                VALUES (?, ?, ?, ?, ?, 'complete', ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                  status='complete',
                  drifted_count=EXCLUDED.drifted_count,
                  total_resources=EXCLUDED.total_resources,
                  exit_code=EXCLUDED.exit_code,
                  summary_json=EXCLUDED.summary_json
            """, (
                report.scan_id, workspace_id, report.created_at,
                report.state_path, report.region,
                summary["drifted"], summary["total_resources"],
                report.exit_code, json.dumps(summary),
            ))
        else:
            conn.execute("""
                INSERT OR REPLACE INTO scans
                  (id, workspace_id, created_at, state_path, region,
                   status, drifted_count, total_resources, exit_code, summary_json)
                VALUES (?, ?, ?, ?, ?, 'complete', ?, ?, ?, ?)
            """, (
                report.scan_id, workspace_id, report.created_at,
                report.state_path, report.region,
                summary["drifted"], summary["total_resources"],
                report.exit_code, json.dumps(summary),
            ))

        for result in report.results:
            rid = str(uuid.uuid4())
            if _is_postgres():
                conn.execute("""
                    INSERT INTO drift_results
                      (id, scan_id, type, resource_id, resource_name,
                       status, attribute_diffs, tag_diffs, remediation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO NOTHING
                """, (
                    rid, report.scan_id, result.type, result.id, result.name,
                    result.status,
                    json.dumps(_serialise_diffs(result.attribute_diffs)),
                    json.dumps(result.tag_diffs), result.remediation,
                ))
            else:
                conn.execute("""
                    INSERT OR IGNORE INTO drift_results
                      (id, scan_id, type, resource_id, resource_name,
                       status, attribute_diffs, tag_diffs, remediation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rid, report.scan_id, result.type, result.id, result.name,
                    result.status,
                    json.dumps(_serialise_diffs(result.attribute_diffs)),
                    json.dumps(result.tag_diffs), result.remediation,
                ))

        if workspace_id:
            conn.execute(
                "UPDATE workspaces SET last_scan_id = ? WHERE id = ?",
                (report.scan_id, workspace_id))

    logger.info("Saved scan %s", report.scan_id)


def get_scan(scan_id: str) -> ScanReport | None:
    """Load a ScanReport from the database by scan_id."""
    with _connect() as conn:
        row = conn.fetchone("SELECT * FROM scans WHERE id = ?", (scan_id,))
        if not row:
            return None
        result_rows = conn.fetchall(
            "SELECT * FROM drift_results WHERE scan_id = ? ORDER BY id",
            (scan_id,))
        workspace_name = None
        if row.get("workspace_id"):
            ws = conn.fetchone(
                "SELECT name FROM workspaces WHERE id = ?",
                (row["workspace_id"],))
            if ws:
                workspace_name = ws["name"]

    return ScanReport(
        scan_id=row["id"],
        created_at=row["created_at"],
        state_path=row["state_path"],
        region=row["region"],
        workspace=workspace_name,
        results=[_row_to_drift_result(r) for r in result_rows],
    )


def list_scans(workspace: str | None = None, limit: int = 20) -> list[dict]:
    """List recent scans, newest first."""
    with _connect() as conn:
        if workspace:
            ws = conn.fetchone(
                "SELECT id FROM workspaces WHERE name = ?", (workspace,))
            ws_id = ws["id"] if ws else None
            rows = conn.fetchall("""
                SELECT s.id, s.created_at, s.state_path, s.region,
                       s.drifted_count, s.total_resources, s.exit_code,
                       s.status, s.summary_json, w.name as workspace_name
                FROM scans s
                LEFT JOIN workspaces w ON s.workspace_id = w.id
                WHERE s.workspace_id = ?
                ORDER BY s.created_at DESC LIMIT ?
            """, (ws_id, limit))
        else:
            rows = conn.fetchall("""
                SELECT s.id, s.created_at, s.state_path, s.region,
                       s.drifted_count, s.total_resources, s.exit_code,
                       s.status, s.summary_json, w.name as workspace_name
                FROM scans s
                LEFT JOIN workspaces w ON s.workspace_id = w.id
                ORDER BY s.created_at DESC LIMIT ?
            """, (limit,))

    return [{
        "scan_id":         r["id"],
        "created_at":      r["created_at"],
        "state_path":      r["state_path"],
        "region":          r["region"],
        "workspace":       r.get("workspace_name"),
        "drifted_count":   r["drifted_count"],
        "total_resources": r["total_resources"],
        "exit_code":       r["exit_code"],
        "status":          r.get("status", "complete"),
        "summary_json":    r.get("summary_json"),
    } for r in rows]


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

def save_workspace(
    name: str, state_path: str, region: str,
    state_backend: str = "local", state_region: str | None = None,
    detect_unmanaged: bool = False, schedule_cron: str | None = None,
    provider: str = "aws",
) -> str:
    """Insert or update a workspace. Returns the workspace id."""
    with _connect() as conn:
        existing = conn.fetchone(
            "SELECT id FROM workspaces WHERE name = ?", (name,))
        if existing:
            ws_id = existing["id"]
            conn.execute("""
                UPDATE workspaces
                SET state_path=?, region=?, state_backend=?,
                    state_region=?, detect_unmanaged=?,
                    schedule_cron=?, provider=?
                WHERE id=?
            """, (state_path, region, state_backend, state_region,
                  int(detect_unmanaged), schedule_cron, provider, ws_id))
        else:
            ws_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO workspaces
                  (id, name, provider, state_backend, state_path,
                   state_region, region, detect_unmanaged,
                   schedule_cron, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ws_id, name, provider, state_backend, state_path,
                  state_region, region, int(detect_unmanaged),
                  schedule_cron, datetime.now(timezone.utc).isoformat()))
    return ws_id


def list_workspaces() -> list[dict]:
    with _connect() as conn:
        return conn.fetchall("SELECT * FROM workspaces ORDER BY name")


def get_workspace_by_name(name: str) -> dict | None:
    with _connect() as conn:
        return conn.fetchone(
            "SELECT * FROM workspaces WHERE name = ?", (name,))


def update_schedule(workspace_name: str, cron: str) -> bool:
    with _connect() as conn:
        existing = conn.fetchone(
            "SELECT id FROM workspaces WHERE name = ?", (workspace_name,))
        if not existing:
            return False
        conn.execute(
            "UPDATE workspaces SET schedule_cron = ? WHERE name = ?",
            (cron, workspace_name))
    return True


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _row_to_drift_result(row: dict) -> DriftResult:
    try:
        attr_diffs = json.loads(row.get("attribute_diffs") or "{}")
    except json.JSONDecodeError:
        attr_diffs = {}
    try:
        tag_diffs = json.loads(row.get("tag_diffs") or "{}")
    except json.JSONDecodeError:
        tag_diffs = {}
    return DriftResult(
        type=row["type"], id=row["resource_id"],
        name=row.get("resource_name"), status=row["status"],
        attribute_diffs=attr_diffs, tag_diffs=tag_diffs,
        remediation=row.get("remediation"),
    )


def _serialise_diffs(diffs: dict) -> dict:
    result = {}
    for field, diff in diffs.items():
        result[field] = {
            "expected": _to_json_safe(diff.get("expected")),
            "actual":   _to_json_safe(diff.get("actual")),
        }
    return result


def _to_json_safe(value: object) -> object:
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
