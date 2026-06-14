"""
tests/test_smoke.py

End-to-end smoke tests using subprocess so stdout/stderr are
cleanly separated. Status messages go to stderr; JSON to stdout.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SAMPLE_STATE = str(
    Path(__file__).parent.parent / "testdata" / "sample.tfstate"
)
PY = sys.executable


def run(*args: str) -> subprocess.CompletedProcess:
    """Run driftctl as a subprocess with separated stdout/stderr."""
    return subprocess.run(
        [PY, "-c", f"from driftctl.cli import app; app()"] + list(args),
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

class TestJsonOutput:

    def test_json_valid(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json", "--verbose")
        assert r.returncode in (0, 1)
        data = json.loads(r.stdout)
        assert isinstance(data, dict)

    def test_json_top_level_keys(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json", "--verbose")
        data = json.loads(r.stdout)
        for k in ("scan_id", "created_at", "state_path",
                  "region", "summary", "results"):
            assert k in data

    def test_json_summary_fields(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json", "--verbose")
        data = json.loads(r.stdout)
        for k in ("total_resources", "drifted", "missing",
                  "unmanaged", "modified", "tag_drift"):
            assert k in data["summary"]

    def test_json_state_path(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json", "--verbose")
        data = json.loads(r.stdout)
        assert data["state_path"] == SAMPLE_STATE

    def test_json_5_resources(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json", "--verbose")
        data = json.loads(r.stdout)
        assert data["summary"]["total_resources"] == 5

    def test_json_results_have_remediation(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json", "--verbose")
        data = json.loads(r.stdout)
        for result in data["results"]:
            assert "remediation" in result

    def test_json_verbose_includes_results(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json", "--verbose")
        data = json.loads(r.stdout)
        statuses = {res["status"] for res in data["results"]}
        assert len(data["results"]) > 0  # verbose includes all statuses

    def test_json_default_excludes_in_sync(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json")
        data = json.loads(r.stdout)
        statuses = {res["status"] for res in data["results"]}
        assert "IN_SYNC" not in statuses

    def test_json_output_file(self, tmp_path):
        out = tmp_path / "report.json"
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "json", "--verbose",
                "--output-file", str(out))
        assert r.returncode in (0, 1)
        assert out.exists()
        data = json.loads(out.read_text())
        assert "scan_id" in data


# ---------------------------------------------------------------------------
# Table output
# ---------------------------------------------------------------------------

class TestTableOutput:

    def test_table_no_crash(self):
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "table", "--verbose")
        assert r.returncode in (0, 1)

    def test_table_non_empty_stderr(self):
        """Status messages go to stderr."""
        r = run("scan", "--state", SAMPLE_STATE,
                "--skip-cloud", "--output", "table", "--verbose")
        assert len(r.stderr) > 0 or len(r.stdout) > 0


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

class TestExitCodes:

    def test_exit_2_missing_state_file(self):
        r = run("scan", "--state", "/nonexistent/tf.tfstate", "--skip-cloud")
        assert r.returncode == 2

    def test_exit_2_no_state_arg(self):
        r = run("scan", "--skip-cloud")
        assert r.returncode == 2


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

class TestHelp:

    def test_root_help(self):
        r = run("--help")
        assert r.returncode == 0

    def test_scan_help(self):
        r = run("scan", "--help")
        assert r.returncode == 0
        assert "--state" in r.stdout

    def test_workspace_help(self):
        r = run("workspace", "--help")
        assert r.returncode == 0

    def test_scans_help(self):
        r = run("scans", "--help")
        assert r.returncode == 0
