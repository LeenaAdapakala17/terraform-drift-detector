"""
driftctl/report/json_renderer.py

Renders a ScanReport as a JSON-serialisable dict.

Used by:
  - CLI --output json
  - CLI --output-file path
  - REST API GET /api/v1/scans/{id}/report?format=json
  - Dashboard data source
"""

from __future__ import annotations

import json
from typing import Any

from driftctl.models import DriftResult, ScanReport


def render_json(report: ScanReport, verbose: bool = False) -> dict:
    """
    Convert a ScanReport to a JSON-serialisable dict.

    Args:
        report:  The scan report to render.
        verbose: When False (default), IN_SYNC results are excluded.
                 When True, all results including IN_SYNC are included.

    Returns:
        A dict that can be passed to json.dumps().
    """
    results = report.results if verbose else report.drifted

    return {
        "scan_id":    report.scan_id,
        "created_at": report.created_at,
        "state_path": report.state_path,
        "region":     report.region,
        "workspace":  report.workspace,
        "summary":    report.summary(),
        "results":    [_serialise_result(r) for r in results],
    }


def render_json_string(
    report: ScanReport,
    verbose: bool = False,
    indent: int = 2,
) -> str:
    """Return the report as a formatted JSON string."""
    return json.dumps(
        render_json(report, verbose=verbose),
        indent=indent,
        default=_json_default,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _serialise_result(result: DriftResult) -> dict:
    """Serialise one DriftResult to a dict."""
    return {
        "type":             result.type,
        "id":               result.id,
        "name":             result.name,
        "status":           result.status,
        "attribute_diffs":  _serialise_diffs(result.attribute_diffs),
        "tag_diffs":        result.tag_diffs,
        "remediation":      result.remediation,
    }


def _serialise_diffs(diffs: dict) -> dict:
    """
    Serialise attribute_diffs, converting non-JSON-native types.
    SGRule dataclasses are converted to dicts.
    Lists of SGRule become lists of dicts.
    """
    serialised = {}
    for field, diff in diffs.items():
        serialised[field] = {
            "expected": _make_serialisable(diff["expected"]),
            "actual":   _make_serialisable(diff["actual"]),
        }
    return serialised


def _make_serialisable(value: Any) -> Any:
    """Recursively convert values to JSON-safe types."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_make_serialisable(v) for v in value]
    if isinstance(value, dict):
        return {k: _make_serialisable(v) for k, v in value.items()}
    # Handle SGRule dataclass and similar — convert to dict
    if hasattr(value, "__dataclass_fields__"):
        return {
            field: _make_serialisable(getattr(value, field))
            for field in value.__dataclass_fields__
        }
    return str(value)


def _json_default(obj: Any) -> Any:
    """Fallback serialiser for json.dumps."""
    if hasattr(obj, "__dataclass_fields__"):
        return {
            field: getattr(obj, field)
            for field in obj.__dataclass_fields__
        }
    return str(obj)
