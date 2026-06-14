"""
tests/test_remediation.py

Unit tests for driftctl/engine/remediate.py

Tests the exact Terraform command string produced for each drift status.
No I/O, no AWS — pure function tests.
"""

from __future__ import annotations

import pytest

from driftctl.engine.remediate import (
    enrich_results,
    generate_remediation,
)
from driftctl.models import DriftResult


# ---------------------------------------------------------------------------
# Fixtures — DriftResult builders per status
# ---------------------------------------------------------------------------

def _missing(name: str | None = "web_server", resource_id: str = "i-0abc123") -> DriftResult:
    return DriftResult(
        type="aws_instance",
        id=resource_id,
        name=name,
        status="MISSING",
        attribute_diffs={},
        tag_diffs={},
    )


def _unmanaged(
    resource_type: str = "aws_instance",
    resource_id: str = "i-0abc123",
) -> DriftResult:
    return DriftResult(
        type=resource_type,
        id=resource_id,
        name=None,
        status="UNMANAGED",
        attribute_diffs={},
        tag_diffs={},
    )


def _modified(
    attribute_diffs: dict | None = None,
    resource_type: str = "aws_instance",
    resource_id: str = "i-0abc123",
    name: str | None = "web_server",
) -> DriftResult:
    return DriftResult(
        type=resource_type,
        id=resource_id,
        name=name,
        status="MODIFIED",
        attribute_diffs=attribute_diffs or {
            "instance_type": {"expected": "t2.micro", "actual": "t3.small"}
        },
        tag_diffs={},
    )


def _tag_drift(tag_diffs: dict | None = None) -> DriftResult:
    return DriftResult(
        type="aws_vpc",
        id="vpc-0abc123",
        name="main",
        status="TAG_DRIFT",
        attribute_diffs={},
        tag_diffs=tag_diffs or {
            "env": {"expected": "production", "actual": "staging"}
        },
    )


def _in_sync() -> DriftResult:
    return DriftResult(
        type="aws_vpc",
        id="vpc-0abc123",
        name="main",
        status="IN_SYNC",
        attribute_diffs={},
        tag_diffs={},
    )


# ---------------------------------------------------------------------------
# IN_SYNC
# ---------------------------------------------------------------------------

class TestInSync:

    def test_in_sync_returns_none(self):
        assert generate_remediation(_in_sync()) is None


# ---------------------------------------------------------------------------
# MISSING
# ---------------------------------------------------------------------------

class TestMissing:

    def test_missing_returns_string(self):
        result = generate_remediation(_missing())
        assert isinstance(result, str)
        assert len(result) > 0

    def test_missing_contains_terraform_apply(self):
        result = generate_remediation(_missing())
        assert "terraform apply" in result

    def test_missing_contains_state_rm(self):
        result = generate_remediation(_missing())
        assert "terraform state rm" in result

    def test_missing_state_rm_uses_name(self):
        """state rm address should use the terraform logical name."""
        result = generate_remediation(_missing(name="web_server"))
        assert "aws_instance.web_server" in result

    def test_missing_state_rm_uses_id_when_no_name(self):
        """When name is None, state rm should derive a name from the resource id."""
        result = generate_remediation(_missing(name=None, resource_id="i-0abc123"))
        # The id is transformed into a suggested name: i-0abc123 → instance_i_0abc123
        assert "0abc123" in result

    def test_missing_has_option_a_and_b(self):
        """Should explain both options clearly."""
        result = generate_remediation(_missing())
        assert "Option A" in result
        assert "Option B" in result


# ---------------------------------------------------------------------------
# UNMANAGED
# ---------------------------------------------------------------------------

class TestUnmanaged:

    def test_unmanaged_returns_string(self):
        result = generate_remediation(_unmanaged())
        assert isinstance(result, str)

    def test_unmanaged_contains_terraform_import(self):
        result = generate_remediation(_unmanaged())
        assert "terraform import" in result

    def test_unmanaged_contains_resource_type(self):
        result = generate_remediation(_unmanaged(resource_type="aws_instance"))
        assert "aws_instance" in result

    def test_unmanaged_contains_resource_id(self):
        result = generate_remediation(_unmanaged(resource_id="i-0abc1234"))
        assert "i-0abc1234" in result

    def test_unmanaged_s3_uses_bucket_name(self):
        result = generate_remediation(_unmanaged(
            resource_type="aws_s3_bucket",
            resource_id="my-prod-bucket",
        ))
        assert "my-prod-bucket" in result
        assert "aws_s3_bucket" in result

    def test_unmanaged_suggested_name_for_instance(self):
        """Suggested name should be derived from instance id."""
        result = generate_remediation(_unmanaged(
            resource_type="aws_instance",
            resource_id="i-0abc1234",
        ))
        assert "instance_" in result

    def test_unmanaged_suggested_name_for_vpc(self):
        result = generate_remediation(_unmanaged(
            resource_type="aws_vpc",
            resource_id="vpc-0abc1234",
        ))
        assert "vpc_" in result

    def test_unmanaged_contains_hcl_hint(self):
        """Should hint to add an HCL resource block."""
        result = generate_remediation(_unmanaged())
        assert "resource" in result.lower()


# ---------------------------------------------------------------------------
# MODIFIED
# ---------------------------------------------------------------------------

class TestModified:

    def test_modified_returns_string(self):
        result = generate_remediation(_modified())
        assert isinstance(result, str)

    def test_modified_contains_terraform_apply(self):
        result = generate_remediation(_modified())
        assert "terraform apply" in result

    def test_modified_lists_changed_field(self):
        """The changed field name should appear in the hint."""
        result = generate_remediation(_modified(
            attribute_diffs={
                "instance_type": {"expected": "t2.micro", "actual": "t3.small"}
            }
        ))
        assert "instance_type" in result

    def test_modified_shows_expected_value(self):
        """The expected (target) value should appear in the hint."""
        result = generate_remediation(_modified(
            attribute_diffs={
                "instance_type": {"expected": "t2.micro", "actual": "t3.small"}
            }
        ))
        assert "t2.micro" in result

    def test_modified_shows_actual_value(self):
        """The actual (current) value should appear in the hint."""
        result = generate_remediation(_modified(
            attribute_diffs={
                "instance_type": {"expected": "t2.micro", "actual": "t3.small"}
            }
        ))
        assert "t3.small" in result

    def test_modified_multiple_fields_all_listed(self):
        """All changed fields should appear in the hint."""
        result = generate_remediation(_modified(
            attribute_diffs={
                "instance_type": {"expected": "t2.micro", "actual": "t3.small"},
                "monitoring":    {"expected": False, "actual": True},
            }
        ))
        assert "instance_type" in result
        assert "monitoring" in result

    def test_modified_shows_arrow_direction(self):
        """Should show actual → expected (the revert direction)."""
        result = generate_remediation(_modified(
            attribute_diffs={
                "instance_type": {"expected": "t2.micro", "actual": "t3.small"}
            }
        ))
        # Arrow should appear in the hint
        assert "→" in result


# ---------------------------------------------------------------------------
# TAG_DRIFT
# ---------------------------------------------------------------------------

class TestTagDrift:

    def test_tag_drift_returns_string(self):
        result = generate_remediation(_tag_drift())
        assert isinstance(result, str)

    def test_tag_drift_contains_terraform_apply(self):
        result = generate_remediation(_tag_drift())
        assert "terraform apply" in result

    def test_tag_drift_lists_changed_tag(self):
        result = generate_remediation(_tag_drift(
            tag_diffs={"env": {"expected": "production", "actual": "staging"}}
        ))
        assert "env" in result

    def test_tag_drift_shows_expected_value(self):
        result = generate_remediation(_tag_drift(
            tag_diffs={"env": {"expected": "production", "actual": "staging"}}
        ))
        assert "production" in result

    def test_tag_drift_shows_actual_value(self):
        result = generate_remediation(_tag_drift(
            tag_diffs={"env": {"expected": "production", "actual": "staging"}}
        ))
        assert "staging" in result

    def test_tag_added_shows_add_hint(self):
        """Tag that exists in cloud but not state."""
        result = generate_remediation(_tag_drift(
            tag_diffs={"NewTag": {"expected": None, "actual": "value"}}
        ))
        assert "NewTag" in result
        assert "remove" in result.lower()

    def test_tag_removed_shows_remove_hint(self):
        """Tag that exists in state but not cloud."""
        result = generate_remediation(_tag_drift(
            tag_diffs={"CostCenter": {"expected": "engineering", "actual": None}}
        ))
        assert "CostCenter" in result
        assert "add" in result.lower()


# ---------------------------------------------------------------------------
# enrich_results
# ---------------------------------------------------------------------------

class TestEnrichResults:

    def test_enrich_populates_remediation(self):
        """enrich_results should set remediation on every result."""
        results = [_missing(), _unmanaged(), _modified(), _tag_drift(), _in_sync()]
        enrich_results(results)
        for r in results:
            if r.status == "IN_SYNC":
                assert r.remediation is None
            else:
                assert r.remediation is not None
                assert isinstance(r.remediation, str)

    def test_enrich_returns_same_list(self):
        """enrich_results should return the same list object."""
        results = [_missing()]
        returned = enrich_results(results)
        assert returned is results

    def test_enrich_empty_list(self):
        """enrich_results on empty list should not raise."""
        result = enrich_results([])
        assert result == []
