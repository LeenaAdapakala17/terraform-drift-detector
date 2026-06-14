"""
tests/test_storage.py

Unit tests for driftctl/storage/db.py and driftctl/config.py

All tests use a temporary SQLite file (tmp_path fixture) so they
never touch the real driftctl.db and never leave state behind.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from driftctl.models import DriftResult, ScanReport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """
    Point the storage layer at a fresh temp DB for every test.
    autouse=True means every test in this module gets isolation
    without having to request the fixture explicitly.
    """
    db_file = str(tmp_path / "test.db")
    import driftctl.storage.db as db_mod
    monkeypatch.setattr(db_mod, "_db_path", db_file)
    yield db_file


def _make_report(
    drifted: bool = False,
    workspace: str | None = None,
) -> ScanReport:
    """Build a minimal ScanReport for testing."""
    results = []
    if drifted:
        results.append(DriftResult(
            type="aws_instance",
            id="i-0abc123",
            name="web_server",
            status="MISSING",
            attribute_diffs={},
            tag_diffs={},
            remediation="terraform apply",
        ))
    else:
        results.append(DriftResult(
            type="aws_vpc",
            id="vpc-001",
            name="main",
            status="IN_SYNC",
            attribute_diffs={},
            tag_diffs={},
            remediation=None,
        ))

    return ScanReport(
        scan_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        state_path="./terraform.tfstate",
        region="us-east-1",
        workspace=workspace,
        results=results,
    )


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

class TestSchema:

    def test_schema_created_on_connect(self, tmp_path):
        """Connecting to a new DB should create all tables."""
        import sqlite3
        import driftctl.storage.db as db_mod
        db_file = str(tmp_path / "schema_test.db")
        db_mod.set_db_path(db_file)
        # Trigger connection
        db_mod.list_scans()
        conn = sqlite3.connect(db_file)
        tables = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        assert "scans" in tables
        assert "drift_results" in tables
        assert "workspaces" in tables


# ---------------------------------------------------------------------------
# Scan persistence
# ---------------------------------------------------------------------------

class TestSaveScan:

    def test_save_and_retrieve_scan(self):
        from driftctl.storage.db import save_scan, get_scan
        report = _make_report()
        save_scan(report)
        retrieved = get_scan(report.scan_id)
        assert retrieved is not None
        assert retrieved.scan_id == report.scan_id

    def test_retrieved_scan_has_correct_state_path(self):
        from driftctl.storage.db import save_scan, get_scan
        report = _make_report()
        save_scan(report)
        retrieved = get_scan(report.scan_id)
        assert retrieved.state_path == report.state_path

    def test_retrieved_scan_has_correct_region(self):
        from driftctl.storage.db import save_scan, get_scan
        report = _make_report()
        save_scan(report)
        retrieved = get_scan(report.scan_id)
        assert retrieved.region == report.region

    def test_retrieved_scan_has_results(self):
        from driftctl.storage.db import save_scan, get_scan
        report = _make_report(drifted=True)
        save_scan(report)
        retrieved = get_scan(report.scan_id)
        assert len(retrieved.results) == 1

    def test_retrieved_result_has_correct_status(self):
        from driftctl.storage.db import save_scan, get_scan
        report = _make_report(drifted=True)
        save_scan(report)
        retrieved = get_scan(report.scan_id)
        assert retrieved.results[0].status == "MISSING"

    def test_retrieved_result_has_remediation(self):
        from driftctl.storage.db import save_scan, get_scan
        report = _make_report(drifted=True)
        save_scan(report)
        retrieved = get_scan(report.scan_id)
        assert retrieved.results[0].remediation == "terraform apply"

    def test_get_nonexistent_scan_returns_none(self):
        from driftctl.storage.db import get_scan
        result = get_scan("nonexistent-scan-id-12345")
        assert result is None

    def test_save_scan_with_attribute_diffs(self):
        """Attribute diffs should be serialised and retrieved correctly."""
        from driftctl.storage.db import save_scan, get_scan
        report = ScanReport(
            scan_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(),
            state_path="./tf.tfstate",
            region="us-east-1",
            workspace=None,
            results=[
                DriftResult(
                    type="aws_instance",
                    id="i-001",
                    name="web",
                    status="MODIFIED",
                    attribute_diffs={
                        "instance_type": {
                            "expected": "t2.micro",
                            "actual": "t3.small",
                        }
                    },
                    tag_diffs={},
                    remediation="terraform apply",
                )
            ],
        )
        save_scan(report)
        retrieved = get_scan(report.scan_id)
        diffs = retrieved.results[0].attribute_diffs
        assert "instance_type" in diffs
        assert diffs["instance_type"]["expected"] == "t2.micro"
        assert diffs["instance_type"]["actual"] == "t3.small"

    def test_save_scan_with_tag_diffs(self):
        from driftctl.storage.db import save_scan, get_scan
        report = ScanReport(
            scan_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(),
            state_path="./tf.tfstate",
            region="us-east-1",
            workspace=None,
            results=[
                DriftResult(
                    type="aws_vpc",
                    id="vpc-001",
                    name="main",
                    status="TAG_DRIFT",
                    attribute_diffs={},
                    tag_diffs={"env": {"expected": "prod", "actual": "staging"}},
                    remediation="terraform apply",
                )
            ],
        )
        save_scan(report)
        retrieved = get_scan(report.scan_id)
        tag_diffs = retrieved.results[0].tag_diffs
        assert "env" in tag_diffs
        assert tag_diffs["env"]["expected"] == "prod"


# ---------------------------------------------------------------------------
# List scans
# ---------------------------------------------------------------------------

class TestListScans:

    def test_list_returns_empty_when_no_scans(self):
        from driftctl.storage.db import list_scans
        assert list_scans() == []

    def test_list_returns_saved_scans(self):
        from driftctl.storage.db import save_scan, list_scans
        save_scan(_make_report())
        save_scan(_make_report())
        scans = list_scans()
        assert len(scans) == 2

    def test_list_respects_limit(self):
        from driftctl.storage.db import save_scan, list_scans
        for _ in range(5):
            save_scan(_make_report())
        scans = list_scans(limit=3)
        assert len(scans) == 3

    def test_list_scan_has_required_fields(self):
        from driftctl.storage.db import save_scan, list_scans
        save_scan(_make_report())
        s = list_scans()[0]
        for key in ("scan_id", "created_at", "state_path",
                    "region", "drifted_count", "exit_code"):
            assert key in s

    def test_list_newest_first(self):
        from driftctl.storage.db import save_scan, list_scans
        import time
        r1 = _make_report()
        time.sleep(0.01)
        r2 = _make_report()
        save_scan(r1)
        save_scan(r2)
        scans = list_scans()
        # Most recent should be first
        assert scans[0]["scan_id"] == r2.scan_id


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------

class TestWorkspaceCrud:

    def test_save_and_list_workspace(self):
        from driftctl.storage.db import save_workspace, list_workspaces
        save_workspace(
            name="prod",
            state_path="s3://my-bucket/prod/terraform.tfstate",
            region="us-east-1",
            state_backend="s3",
        )
        workspaces = list_workspaces()
        assert len(workspaces) == 1
        assert workspaces[0]["name"] == "prod"

    def test_save_workspace_returns_id(self):
        from driftctl.storage.db import save_workspace
        ws_id = save_workspace(
            name="staging",
            state_path="./staging.tfstate",
            region="us-east-1",
        )
        assert ws_id is not None
        assert len(ws_id) > 0

    def test_save_workspace_upsert(self):
        """Saving same workspace name twice should update, not duplicate."""
        from driftctl.storage.db import save_workspace, list_workspaces
        save_workspace(name="dev", state_path="./dev.tfstate", region="us-east-1")
        save_workspace(name="dev", state_path="./dev-new.tfstate", region="us-west-2")
        workspaces = list_workspaces()
        assert len(workspaces) == 1
        assert workspaces[0]["region"] == "us-west-2"

    def test_get_workspace_by_name(self):
        from driftctl.storage.db import save_workspace, get_workspace_by_name
        save_workspace(name="myws", state_path="./tf.tfstate", region="us-east-1")
        ws = get_workspace_by_name("myws")
        assert ws is not None
        assert ws["name"] == "myws"

    def test_get_nonexistent_workspace_returns_none(self):
        from driftctl.storage.db import get_workspace_by_name
        assert get_workspace_by_name("doesnotexist") is None

    def test_list_empty_workspaces(self):
        from driftctl.storage.db import list_workspaces
        assert list_workspaces() == []

    def test_workspace_with_schedule(self):
        from driftctl.storage.db import save_workspace, get_workspace_by_name
        save_workspace(
            name="scheduled",
            state_path="./tf.tfstate",
            region="us-east-1",
            schedule_cron="0 6 * * *",
        )
        ws = get_workspace_by_name("scheduled")
        assert ws["schedule_cron"] == "0 6 * * *"

    def test_update_schedule(self):
        from driftctl.storage.db import save_workspace, update_schedule, get_workspace_by_name
        save_workspace(name="upd", state_path="./tf.tfstate", region="us-east-1")
        result = update_schedule("upd", "0 */6 * * *")
        assert result is True
        ws = get_workspace_by_name("upd")
        assert ws["schedule_cron"] == "0 */6 * * *"

    def test_update_schedule_nonexistent_returns_false(self):
        from driftctl.storage.db import update_schedule
        result = update_schedule("doesnotexist", "0 6 * * *")
        assert result is False


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

class TestConfigLoader:

    def test_load_default_config_when_no_file(self):
        from driftctl.config import load_config
        cfg = load_config(None)
        assert cfg.database == "driftctl.db"
        assert cfg.default_region == "us-east-1"

    def test_load_config_from_yaml(self, tmp_path):
        from driftctl.config import load_config
        config_file = tmp_path / "test.yaml"
        config_file.write_text("""
database: /tmp/mydb.db
default_region: eu-west-1
api:
  addr: ":9090"
  api_key: "secret"
workspaces:
  - name: prod
    provider: aws
    state:
      backend: s3
      bucket: my-bucket
      key: prod/terraform.tfstate
      region: us-east-1
    regions: [us-east-1]
    schedule:
      cron: "0 6 * * *"
""", encoding="utf-8")
        cfg = load_config(str(config_file))
        assert cfg.database == "/tmp/mydb.db"
        assert cfg.default_region == "eu-west-1"
        assert cfg.api.port == 9090
        assert cfg.api.api_key == "secret"
        assert len(cfg.workspaces) == 1
        assert cfg.workspaces[0].name == "prod"
        assert cfg.workspaces[0].schedule_cron == "0 6 * * *"

    def test_config_workspace_s3_uri(self, tmp_path):
        from driftctl.config import load_config
        config_file = tmp_path / "test.yaml"
        config_file.write_text("""
workspaces:
  - name: prod
    provider: aws
    state:
      backend: s3
      bucket: my-bucket
      key: prod/terraform.tfstate
      region: us-east-1
    regions: [us-east-1]
""", encoding="utf-8")
        cfg = load_config(str(config_file))
        ws = cfg.get_workspace("prod")
        assert ws is not None
        assert ws.state.source_uri() == "s3://my-bucket/prod/terraform.tfstate"

    def test_config_workspace_local_uri(self, tmp_path):
        from driftctl.config import load_config
        config_file = tmp_path / "test.yaml"
        config_file.write_text("""
workspaces:
  - name: dev
    provider: aws
    state:
      backend: local
      path: ./terraform.tfstate
    regions: [us-east-1]
""", encoding="utf-8")
        cfg = load_config(str(config_file))
        ws = cfg.get_workspace("dev")
        assert ws.state.source_uri() == "./terraform.tfstate"

    def test_config_get_workspace_none_when_missing(self, tmp_path):
        from driftctl.config import load_config
        cfg = load_config(None)
        assert cfg.get_workspace("nonexistent") is None

    def test_config_api_host_port(self, tmp_path):
        from driftctl.config import load_config
        config_file = tmp_path / "test.yaml"
        config_file.write_text("api:\n  addr: ':8080'\n", encoding="utf-8")
        cfg = load_config(str(config_file))
        assert cfg.api.host == "0.0.0.0"
        assert cfg.api.port == 8080

    def test_config_missing_file_uses_defaults(self, tmp_path):
        from driftctl.config import load_config
        cfg = load_config(str(tmp_path / "nonexistent.yaml"))
        assert cfg.default_region == "us-east-1"

    def test_env_var_overrides_config(self, tmp_path, monkeypatch):
        from driftctl.config import load_config
        monkeypatch.setenv("DRIFTCTL_REGION", "ap-southeast-1")
        cfg = load_config(None)
        assert cfg.default_region == "ap-southeast-1"
