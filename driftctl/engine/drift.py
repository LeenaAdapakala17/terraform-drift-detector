"""
driftctl/engine/drift.py

The Drift Engine.

Pure function — no I/O, no AWS, no filesystem reads.
Takes two lists of Resource and returns a list of DriftResult.

Algorithm:
  1. Index both sides by (type, id)
  2. MISSING  — in expected, not in actual
  3. UNMANAGED — in actual, not in expected (only if detect_unmanaged=True)
  4. Compare shared resources field by field → MODIFIED / TAG_DRIFT / IN_SYNC
"""

from __future__ import annotations

import logging

from driftctl.models import DriftResult, Resource

logger = logging.getLogger(__name__)


def detect_drift(
    expected: list[Resource],
    actual: list[Resource],
    detect_unmanaged: bool = False,
) -> list[DriftResult]:
    """
    Compare expected (from tfstate) against actual (from AWS APIs).

    Args:
        expected:          Resources extracted from the Terraform state file.
        actual:            Resources fetched from live AWS APIs.
        detect_unmanaged:  When True, resources in actual but not in expected
                           are reported as UNMANAGED. Default False to avoid
                           noise on shared accounts.

    Returns:
        List of DriftResult, one per unique (type, id) pair evaluated.
        IN_SYNC results are included — renderers filter them unless --verbose.
        remediation field is None at this stage; set by remediate.py after.
    """
    # Step 1 — Index both sides on (type, id)
    expected_index: dict[tuple[str, str], Resource] = {
        (r.type, r.id): r for r in expected
    }
    actual_index: dict[tuple[str, str], Resource] = {
        (r.type, r.id): r for r in actual
    }

    results: list[DriftResult] = []

    # Step 2 — MISSING: in expected, gone from cloud
    for key, exp in expected_index.items():
        if key not in actual_index:
            logger.debug("MISSING: %s %s", exp.type, exp.id)
            results.append(DriftResult(
                type=exp.type,
                id=exp.id,
                name=exp.name,
                status="MISSING",
                attribute_diffs={},
                tag_diffs={},
                remediation=None,
            ))

    # Step 3 — UNMANAGED: in cloud, not in state
    if detect_unmanaged:
        for key, act in actual_index.items():
            if key not in expected_index:
                logger.debug("UNMANAGED: %s %s", act.type, act.id)
                results.append(DriftResult(
                    type=act.type,
                    id=act.id,
                    name=None,
                    status="UNMANAGED",
                    attribute_diffs={},
                    tag_diffs={},
                    remediation=None,
                ))

    # Step 4 — Compare shared resources
    for key in expected_index:
        if key not in actual_index:
            continue  # already handled as MISSING

        exp = expected_index[key]
        act = actual_index[key]

        attr_diffs = _diff_attributes(exp.attributes, act.attributes)
        tag_diffs  = _diff_tags(exp.tags, act.tags)

        if not attr_diffs and not tag_diffs:
            status = "IN_SYNC"
        elif attr_diffs:
            status = "MODIFIED"
        else:
            status = "TAG_DRIFT"

        logger.debug("%s: %s %s", status, exp.type, exp.id)
        results.append(DriftResult(
            type=exp.type,
            id=exp.id,
            name=exp.name,
            status=status,
            attribute_diffs=attr_diffs,
            tag_diffs=tag_diffs,
            remediation=None,
        ))

    logger.info(
        "Drift engine: %d resources evaluated, %d drifted",
        len(results),
        sum(1 for r in results if r.status != "IN_SYNC"),
    )
    return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _diff_attributes(
    expected: dict,
    actual: dict,
) -> dict:
    """
    Compare two normalised attribute dicts field by field.

    Returns a dict of fields that differ:
        {field_name: {"expected": <value>, "actual": <value>}}

    Only fields present in expected are compared. Fields present in actual
    but not in expected are not flagged — that is the UNMANAGED concern,
    handled at the resource level, not the field level.

    List fields (e.g. vpc_security_group_ids, SG rule lists) are already
    sorted by the extractors, so we compare with == directly.

    None vs missing key: both treated as None to avoid false positives
    when one side omits a field that the other represents as None.
    """
    diffs = {}
    for field, exp_val in expected.items():
        act_val = actual.get(field)

        # Normalise: treat missing key same as None
        if exp_val is None and act_val is None:
            continue

        if exp_val != act_val:
            diffs[field] = {
                "expected": exp_val,
                "actual":   act_val,
            }
    return diffs


def _diff_tags(
    expected: dict[str, str],
    actual: dict[str, str],
) -> dict:
    """
    Compare two tag dicts.

    Returns a dict of tags that differ (added, removed, or changed):
        {tag_key: {"expected": <value or None>, "actual": <value or None>}}

    None on either side means the tag was absent.
    """
    diffs = {}
    all_keys = set(expected) | set(actual)

    for key in all_keys:
        exp_val = expected.get(key)
        act_val = actual.get(key)
        if exp_val != act_val:
            diffs[key] = {
                "expected": exp_val,
                "actual":   act_val,
            }
    return diffs
