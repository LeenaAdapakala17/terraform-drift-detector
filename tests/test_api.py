"""
tests/test_api.py

Integration tests for the REST API using FastAPI's TestClient.

All tests use an isolated temporary SQLite database (no real driftctl.db).
No real AWS calls — scan triggers are tested for acceptance only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from driftctl.models import DriftResult, ScanReport

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isolated temp database for each test."""
    import driftctl.storage.db as db_mod
    db_file = str(tmp_path / "test_api.db")
    monkeypatch.setattr(db_mod, "_db_path", db_file)
    return db_file


@pytest.fixture()
def client(db):
    """TestClient wired to a fresh app + isolated DB."""
    from driftctl.api.server import create_app
    app = create_app(api_key="")
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def client_with_key(db):
    """TestClient with API key auth enabled."""
    from driftctl.api.server import create_app
    app = create_app(api_key="test-secret")
    return TestClient(app, raise_server_exceptions=True)


def _seed_workspace(client: TestClient, name: str = "prod") -> dict:
    """Helper: create a workspace and return the response JSON."""
    resp = client.post("/api/v1/workspaces", json={
        "name": name,
        "state_path": "./terraform.tfstate",
        "region": "us-east-1",
        "state_backend": "local",
    })
    assert resp.status_code == 201
    return resp.json()["data"]


def _seed_scan(scan_id: str | None = None, workspace: str | None = None) -> ScanReport:
    """Helper: save a scan directly to DB and return the report."""
    from driftctl.storage.db import save_scan
    report = ScanReport(
        scan_id=scan_id or str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        state_path="./terraform.tfstate",
        region="us-east-1",
        workspace=workspace,
        results=[
            DriftResult(
                type="aws_instance",
                id="i-0abc123",
                name="web_server",
                status="MISSING",
                attribute_diffs={},
                tag_diffs={},
                remediation="terraform apply",
            )
        ],
    )
    save_scan(report)
    return report


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:

    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_body(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_health_no_auth_required(self, client_with_key):
        """Health check should be public even when API key is set."""
        resp = client_with_key.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/workspaces
# ---------------------------------------------------------------------------

class TestListWorkspaces:

    def test_empty_list(self, client):
        resp = client.get("/api/v1/workspaces")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_returns_created_workspace(self, client):
        _seed_workspace(client)
        resp = client.get("/api/v1/workspaces")
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["name"] == "prod"

    def test_response_envelope(self, client):
        resp = client.get("/api/v1/workspaces")
        body = resp.json()
        assert "data" in body
        assert "error" in body
        assert body["error"] is None


# ---------------------------------------------------------------------------
# POST /api/v1/workspaces
# ---------------------------------------------------------------------------

class TestCreateWorkspace:

    def test_create_returns_201(self, client):
        resp = client.post("/api/v1/workspaces", json={
            "name": "staging",
            "state_path": "./staging.tfstate",
            "region": "us-east-1",
        })
        assert resp.status_code == 201

    def test_create_returns_workspace_data(self, client):
        resp = client.post("/api/v1/workspaces", json={
            "name": "dev",
            "state_path": "./dev.tfstate",
            "region": "eu-west-1",
        })
        ws = resp.json()["data"]
        assert ws["name"] == "dev"
        assert ws["region"] == "eu-west-1"

    def test_create_duplicate_returns_409(self, client):
        _seed_workspace(client, "prod")
        resp = client.post("/api/v1/workspaces", json={
            "name": "prod",
            "state_path": "./tf.tfstate",
            "region": "us-east-1",
        })
        assert resp.status_code == 409

    def test_create_with_s3_backend(self, client):
        resp = client.post("/api/v1/workspaces", json={
            "name": "prod-s3",
            "state_path": "s3://my-bucket/prod/terraform.tfstate",
            "region": "us-east-1",
            "state_backend": "s3",
        })
        assert resp.status_code == 201
        ws = resp.json()["data"]
        assert ws["state_backend"] == "s3"

    def test_create_with_schedule(self, client):
        resp = client.post("/api/v1/workspaces", json={
            "name": "scheduled",
            "state_path": "./tf.tfstate",
            "region": "us-east-1",
            "schedule_cron": "0 6 * * *",
        })
        assert resp.status_code == 201
        ws = resp.json()["data"]
        assert ws["schedule_cron"] == "0 6 * * *"


# ---------------------------------------------------------------------------
# GET /api/v1/workspaces/{id}
# ---------------------------------------------------------------------------

class TestGetWorkspace:

    def test_get_existing_workspace(self, client):
        created = _seed_workspace(client)
        resp = client.get(f"/api/v1/workspaces/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "prod"

    def test_get_nonexistent_returns_404(self, client):
        resp = client.get("/api/v1/workspaces/nonexistent-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/workspaces/{id}/scans
# ---------------------------------------------------------------------------

class TestTriggerScan:

    def test_trigger_returns_202(self, client):
        ws = _seed_workspace(client)
        resp = client.post(f"/api/v1/workspaces/{ws['id']}/scans")
        assert resp.status_code == 202

    def test_trigger_returns_scan_id(self, client):
        ws = _seed_workspace(client)
        resp = client.post(f"/api/v1/workspaces/{ws['id']}/scans")
        data = resp.json()["data"]
        assert "scan_id" in data
        assert len(data["scan_id"]) > 0

    def test_trigger_returns_running_status(self, client):
        ws = _seed_workspace(client)
        resp = client.post(f"/api/v1/workspaces/{ws['id']}/scans")
        assert resp.json()["data"]["status"] == "running"

    def test_trigger_nonexistent_workspace_404(self, client):
        resp = client.post("/api/v1/workspaces/nonexistent/scans")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/scans
# ---------------------------------------------------------------------------

class TestListScans:

    def test_empty_list(self, client):
        resp = client.get("/api/v1/scans")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_returns_saved_scans(self, client, db):
        _seed_scan()
        resp = client.get("/api/v1/scans")
        assert len(resp.json()["data"]) == 1

    def test_limit_parameter(self, client, db):
        for _ in range(5):
            _seed_scan()
        resp = client.get("/api/v1/scans?limit=3")
        assert len(resp.json()["data"]) == 3

    def test_workspace_filter(self, client, db):
        _seed_workspace(client, "prod")
        _seed_scan(workspace="prod")
        _seed_scan(workspace=None)
        resp = client.get("/api/v1/scans?workspace=prod")
        data = resp.json()["data"]
        assert len(data) == 1


# ---------------------------------------------------------------------------
# GET /api/v1/scans/{id}/report
# ---------------------------------------------------------------------------

class TestGetReport:

    def test_report_json_format(self, client, db):
        report = _seed_scan()
        resp = client.get(f"/api/v1/scans/{report.scan_id}/report?format=json")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "results" in data
        assert "summary" in data

    def test_report_has_remediation(self, client, db):
        report = _seed_scan()
        resp = client.get(f"/api/v1/scans/{report.scan_id}/report?format=json")
        results = resp.json()["data"]["results"]
        assert len(results) == 1
        assert results[0]["remediation"] == "terraform apply"

    def test_report_table_format(self, client, db):
        report = _seed_scan()
        resp = client.get(f"/api/v1/scans/{report.scan_id}/report?format=table")
        assert resp.status_code == 200
        assert isinstance(resp.text, str)
        assert len(resp.text) > 0

    def test_report_nonexistent_returns_404(self, client):
        resp = client.get("/api/v1/scans/nonexistent/report")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/v1/workspaces/{id}/schedules
# ---------------------------------------------------------------------------

class TestUpdateSchedule:

    def test_update_schedule_returns_200(self, client):
        ws = _seed_workspace(client)
        resp = client.put(
            f"/api/v1/workspaces/{ws['id']}/schedules",
            json={"cron": "0 */6 * * *"},
        )
        assert resp.status_code == 200

    def test_update_schedule_persisted(self, client, db):
        ws = _seed_workspace(client)
        client.put(
            f"/api/v1/workspaces/{ws['id']}/schedules",
            json={"cron": "0 9 * * 1"},
        )
        from driftctl.storage.db import get_workspace_by_name
        updated = get_workspace_by_name("prod")
        assert updated["schedule_cron"] == "0 9 * * 1"

    def test_update_schedule_nonexistent_404(self, client):
        resp = client.put(
            "/api/v1/workspaces/nonexistent/schedules",
            json={"cron": "0 6 * * *"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/scans/{id}/summary
# ---------------------------------------------------------------------------

class TestGetScanSummary:

    def test_summary_returns_200(self, client, db):
        report = _seed_scan()
        resp = client.get(f"/api/v1/scans/{report.scan_id}/summary")
        assert resp.status_code == 200

    def test_summary_has_required_fields(self, client, db):
        report = _seed_scan()
        resp = client.get(f"/api/v1/scans/{report.scan_id}/summary")
        data = resp.json()["data"]
        for key in ("scan_id", "status", "drifted_count", "exit_code"):
            assert key in data

    def test_summary_nonexistent_404(self, client):
        resp = client.get("/api/v1/scans/nonexistent/summary")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------

class TestApiKeyAuth:

    def test_no_key_rejected_when_auth_enabled(self, client_with_key):
        resp = client_with_key.get("/api/v1/workspaces")
        assert resp.status_code == 401

    def test_wrong_key_rejected(self, client_with_key):
        resp = client_with_key.get(
            "/api/v1/workspaces",
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_correct_key_accepted(self, client_with_key):
        resp = client_with_key.get(
            "/api/v1/workspaces",
            headers={"X-API-Key": "test-secret"},
        )
        assert resp.status_code == 200

    def test_health_always_public(self, client_with_key):
        resp = client_with_key.get("/health")
        assert resp.status_code == 200
