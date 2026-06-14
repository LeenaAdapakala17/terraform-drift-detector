"""
tests/test_drift_engine.py

Unit tests for driftctl/engine/drift.py

Tests every drift status, edge cases, and the detect_unmanaged flag.
No I/O, no AWS — pure function tests with hand-built Resource lists.
"""

from __future__ import annotations

import pytest

from driftctl.engine.drift import detect_drift
from driftctl.models import Resource


# ---------------------------------------------------------------------------
# Fixtures — reusable Resource builders
# ---------------------------------------------------------------------------

def _make_resource(
    resource_type: str = "aws_vpc",
    resource_id: str = "vpc-001",
    name: str | None = "main",
    attributes: dict | None = None,
    tags: dict | None = None,
    source: str = "expected",
) -> Resource:
    return Resource(
        type=resource_type,
        id=resource_id,
        name=name,
        attributes=attributes or {"cidr_block": "10.0.0.0/16", "instance_tenancy": "default",
                                   "enable_dns_support": True, "enable_dns_hostnames": False},
        tags=tags or {"env": "production"},
        source=source,
    )


def _make_actual(**kwargs) -> Resource:
    kwargs.setdefault("source", "actual")
    kwargs.setdefault("name", None)
    return _make_resource(**kwargs)


# ---------------------------------------------------------------------------
# MISSING — in expected, not in actual
# ---------------------------------------------------------------------------

class TestMissing:

    def test_missing_resource_detected(self):
        expected = [_make_resource()]
        actual = []
        results = detect_drift(expected, actual)
        assert len(results) == 1
        assert results[0].status == "MISSING"

    def test_missing_has_correct_type_and_id(self):
        expected = [_make_resource(resource_type="aws_instance", resource_id="i-001")]
        results = detect_drift(expected, [])
        r = results[0]
        assert r.type == "aws_instance"
        assert r.id == "i-001"

    def test_missing_preserves_name(self):
        expected = [_make_resource(name="web_server")]
        results = detect_drift(expected, [])
        assert results[0].name == "web_server"

    def test_missing_has_empty_diffs(self):
        expected = [_make_resource()]
        results = detect_drift(expected, [])
        assert results[0].attribute_diffs == {}
        assert results[0].tag_diffs == {}

    def test_multiple_missing(self):
        expected = [
            _make_resource(resource_id="vpc-001"),
            _make_resource(resource_id="vpc-002"),
            _make_resource(resource_id="vpc-003"),
        ]
        results = detect_drift(expected, [])
        missing = [r for r in results if r.status == "MISSING"]
        assert len(missing) == 3


# ---------------------------------------------------------------------------
# UNMANAGED — in actual, not in expected
# ---------------------------------------------------------------------------

class TestUnmanaged:

    def test_unmanaged_detected_when_enabled(self):
        actual = [_make_actual(resource_id="vpc-999")]
        results = detect_drift([], actual, detect_unmanaged=True)
        assert len(results) == 1
        assert results[0].status == "UNMANAGED"

    def test_unmanaged_suppressed_by_default(self):
        actual = [_make_actual(resource_id="vpc-999")]
        results = detect_drift([], actual, detect_unmanaged=False)
        assert results == []

    def test_unmanaged_default_is_false(self):
        """detect_unmanaged defaults to False."""
        actual = [_make_actual(resource_id="vpc-999")]
        results = detect_drift([], actual)
        assert results == []

    def test_unmanaged_name_is_none(self):
        """UNMANAGED resources have no terraform logical name."""
        actual = [_make_actual(resource_id="vpc-999")]
        results = detect_drift([], actual, detect_unmanaged=True)
        assert results[0].name is None

    def test_unmanaged_has_empty_diffs(self):
        actual = [_make_actual(resource_id="vpc-999")]
        results = detect_drift([], actual, detect_unmanaged=True)
        assert results[0].attribute_diffs == {}
        assert results[0].tag_diffs == {}

    def test_multiple_unmanaged(self):
        actual = [
            _make_actual(resource_id="sg-aaa"),
            _make_actual(resource_id="sg-bbb"),
        ]
        results = detect_drift([], actual, detect_unmanaged=True)
        unmanaged = [r for r in results if r.status == "UNMANAGED"]
        assert len(unmanaged) == 2


# ---------------------------------------------------------------------------
# MODIFIED — in both, attributes differ
# ---------------------------------------------------------------------------

class TestModified:

    def test_modified_detected(self):
        expected = [_make_resource(
            attributes={"instance_type": "t2.micro", "ami": "ami-001"},
            resource_type="aws_instance", resource_id="i-001",
        )]
        actual = [_make_actual(
            attributes={"instance_type": "t3.small", "ami": "ami-001"},
            resource_type="aws_instance", resource_id="i-001",
        )]
        results = detect_drift(expected, actual)
        assert len(results) == 1
        assert results[0].status == "MODIFIED"

    def test_modified_attribute_diffs_correct(self):
        expected = [_make_resource(
            attributes={"instance_type": "t2.micro", "ami": "ami-001"},
            resource_type="aws_instance", resource_id="i-001",
        )]
        actual = [_make_actual(
            attributes={"instance_type": "t3.small", "ami": "ami-001"},
            resource_type="aws_instance", resource_id="i-001",
        )]
        results = detect_drift(expected, actual)
        diffs = results[0].attribute_diffs
        assert "instance_type" in diffs
        assert diffs["instance_type"]["expected"] == "t2.micro"
        assert diffs["instance_type"]["actual"] == "t3.small"
        # ami is the same — should not be in diffs
        assert "ami" not in diffs

    def test_modified_multiple_fields(self):
        expected = [_make_resource(
            attributes={"instance_type": "t2.micro", "monitoring": False, "ami": "ami-001"},
            resource_type="aws_instance", resource_id="i-001",
        )]
        actual = [_make_actual(
            attributes={"instance_type": "t3.small", "monitoring": True, "ami": "ami-001"},
            resource_type="aws_instance", resource_id="i-001",
        )]
        results = detect_drift(expected, actual)
        diffs = results[0].attribute_diffs
        assert len(diffs) == 2
        assert "instance_type" in diffs
        assert "monitoring" in diffs

    def test_modified_tag_diffs_empty_when_only_attrs_differ(self):
        expected = [_make_resource(
            attributes={"cidr_block": "10.0.0.0/16"},
            tags={"env": "prod"},
        )]
        actual = [_make_actual(
            attributes={"cidr_block": "10.0.1.0/16"},
            tags={"env": "prod"},
        )]
        results = detect_drift(expected, actual)
        assert results[0].status == "MODIFIED"
        assert results[0].tag_diffs == {}

    def test_modified_when_both_attrs_and_tags_differ(self):
        """When both attrs and tags differ, status is MODIFIED (not TAG_DRIFT)."""
        expected = [_make_resource(
            attributes={"cidr_block": "10.0.0.0/16"},
            tags={"env": "prod"},
        )]
        actual = [_make_actual(
            attributes={"cidr_block": "10.0.1.0/16"},
            tags={"env": "staging"},
        )]
        results = detect_drift(expected, actual)
        assert results[0].status == "MODIFIED"
        assert len(results[0].attribute_diffs) > 0
        assert len(results[0].tag_diffs) > 0


# ---------------------------------------------------------------------------
# TAG_DRIFT — in both, only tags differ
# ---------------------------------------------------------------------------

class TestTagDrift:

    def test_tag_drift_detected(self):
        expected = [_make_resource(tags={"env": "production", "Owner": "team-a"})]
        actual = [_make_actual(tags={"env": "staging", "Owner": "team-a"})]
        results = detect_drift(expected, actual)
        assert results[0].status == "TAG_DRIFT"

    def test_tag_drift_diffs_correct(self):
        expected = [_make_resource(tags={"env": "production", "Owner": "team-a"})]
        actual = [_make_actual(tags={"env": "staging", "Owner": "team-a"})]
        results = detect_drift(expected, actual)
        tag_diffs = results[0].tag_diffs
        assert "env" in tag_diffs
        assert tag_diffs["env"]["expected"] == "production"
        assert tag_diffs["env"]["actual"] == "staging"
        assert "Owner" not in tag_diffs  # unchanged

    def test_tag_drift_added_tag(self):
        """Tag added in cloud but not in state."""
        expected = [_make_resource(tags={"env": "prod"})]
        actual = [_make_actual(tags={"env": "prod", "ExtraTag": "value"})]
        results = detect_drift(expected, actual)
        assert results[0].status == "TAG_DRIFT"
        diffs = results[0].tag_diffs
        assert "ExtraTag" in diffs
        assert diffs["ExtraTag"]["expected"] is None
        assert diffs["ExtraTag"]["actual"] == "value"

    def test_tag_drift_removed_tag(self):
        """Tag in state but not in cloud."""
        expected = [_make_resource(tags={"env": "prod", "CostCenter": "eng"})]
        actual = [_make_actual(tags={"env": "prod"})]
        results = detect_drift(expected, actual)
        assert results[0].status == "TAG_DRIFT"
        diffs = results[0].tag_diffs
        assert "CostCenter" in diffs
        assert diffs["CostCenter"]["expected"] == "eng"
        assert diffs["CostCenter"]["actual"] is None

    def test_tag_drift_attribute_diffs_empty(self):
        """TAG_DRIFT should have empty attribute_diffs."""
        expected = [_make_resource(tags={"env": "prod"})]
        actual = [_make_actual(tags={"env": "staging"})]
        results = detect_drift(expected, actual)
        assert results[0].attribute_diffs == {}


# ---------------------------------------------------------------------------
# IN_SYNC — identical on both sides
# ---------------------------------------------------------------------------

class TestInSync:

    def test_in_sync_detected(self):
        attrs = {"cidr_block": "10.0.0.0/16", "instance_tenancy": "default",
                 "enable_dns_support": True, "enable_dns_hostnames": False}
        tags = {"env": "production"}
        expected = [_make_resource(attributes=attrs, tags=tags)]
        actual = [_make_actual(attributes=attrs, tags=tags)]
        results = detect_drift(expected, actual)
        assert len(results) == 1
        assert results[0].status == "IN_SYNC"

    def test_in_sync_has_empty_diffs(self):
        attrs = {"cidr_block": "10.0.0.0/16"}
        expected = [_make_resource(attributes=attrs, tags={})]
        actual = [_make_actual(attributes=attrs, tags={})]
        results = detect_drift(expected, actual)
        r = results[0]
        assert r.attribute_diffs == {}
        assert r.tag_diffs == {}

    def test_in_sync_included_in_results(self):
        """IN_SYNC results ARE included in the return list."""
        attrs = {"cidr_block": "10.0.0.0/16"}
        expected = [_make_resource(attributes=attrs, tags={})]
        actual = [_make_actual(attributes=attrs, tags={})]
        results = detect_drift(expected, actual)
        assert len(results) == 1
        assert results[0].status == "IN_SYNC"


# ---------------------------------------------------------------------------
# Mixed scenarios
# ---------------------------------------------------------------------------

class TestMixedScenarios:

    def test_mix_of_all_statuses(self):
        """One resource of each status in one scan."""
        # MISSING
        exp_missing = _make_resource(resource_id="vpc-missing", name="missing_vpc")
        # IN_SYNC
        attrs_sync = {"cidr_block": "10.1.0.0/16", "instance_tenancy": "default",
                      "enable_dns_support": True, "enable_dns_hostnames": False}
        exp_sync = _make_resource(resource_id="vpc-sync", attributes=attrs_sync, tags={})
        act_sync = _make_actual(resource_id="vpc-sync", attributes=attrs_sync, tags={})
        # MODIFIED
        exp_mod = _make_resource(resource_id="vpc-mod",
                                 attributes={"cidr_block": "10.2.0.0/16"}, tags={})
        act_mod = _make_actual(resource_id="vpc-mod",
                               attributes={"cidr_block": "10.2.1.0/16"}, tags={})
        # TAG_DRIFT
        exp_tag = _make_resource(resource_id="vpc-tag",
                                 attributes={"cidr_block": "10.3.0.0/16"},
                                 tags={"env": "prod"})
        act_tag = _make_actual(resource_id="vpc-tag",
                               attributes={"cidr_block": "10.3.0.0/16"},
                               tags={"env": "staging"})
        # UNMANAGED (cloud-only)
        act_unmanaged = _make_actual(resource_id="vpc-unmanaged")

        expected = [exp_missing, exp_sync, exp_mod, exp_tag]
        actual   = [act_sync, act_mod, act_tag, act_unmanaged]

        results = detect_drift(expected, actual, detect_unmanaged=True)
        statuses = {r.id: r.status for r in results}

        assert statuses["vpc-missing"]   == "MISSING"
        assert statuses["vpc-sync"]      == "IN_SYNC"
        assert statuses["vpc-mod"]       == "MODIFIED"
        assert statuses["vpc-tag"]       == "TAG_DRIFT"
        assert statuses["vpc-unmanaged"] == "UNMANAGED"

    def test_empty_expected_and_actual(self):
        results = detect_drift([], [])
        assert results == []

    def test_empty_expected_actual_has_resources(self):
        """With detect_unmanaged=False, nothing from cloud-only resources."""
        actual = [_make_actual(resource_id="vpc-001")]
        results = detect_drift([], actual, detect_unmanaged=False)
        assert results == []

    def test_empty_actual_all_missing(self):
        expected = [
            _make_resource(resource_id="vpc-001"),
            _make_resource(resource_id="vpc-002"),
        ]
        results = detect_drift(expected, [], detect_unmanaged=True)
        assert all(r.status == "MISSING" for r in results)
        assert len(results) == 2

    def test_different_types_not_matched(self):
        """Resources with same id but different types are not paired."""
        exp = _make_resource(resource_type="aws_vpc", resource_id="same-id")
        act = _make_actual(resource_type="aws_subnet", resource_id="same-id")
        results = detect_drift([exp], [act], detect_unmanaged=True)
        statuses = [r.status for r in results]
        assert "MISSING" in statuses
        assert "UNMANAGED" in statuses
        assert "IN_SYNC" not in statuses

    def test_sorted_list_fields_no_false_drift(self):
        """Sorted list fields (e.g. sg_ids) that are equal must be IN_SYNC."""
        attrs = {"vpc_security_group_ids": ["sg-aaa", "sg-bbb", "sg-ccc"]}
        exp = _make_resource(resource_type="aws_instance", resource_id="i-001",
                             attributes=attrs, tags={})
        # Same IDs, same order (extractors sort both sides)
        act = _make_actual(resource_type="aws_instance", resource_id="i-001",
                           attributes=attrs, tags={})
        results = detect_drift([exp], [act])
        assert results[0].status == "IN_SYNC"

    def test_remediation_is_none_before_enrichment(self):
        """Drift engine sets remediation=None; remediate.py fills it in."""
        expected = [_make_resource()]
        actual = []
        results = detect_drift(expected, actual)
        assert results[0].remediation is None
