"""
driftctl/engine/remediate.py

★ YOUR ADDITION — not in Abhishek's original project.

Remediation Hints Generator.

Pure function — no I/O, no AWS, no filesystem.
Takes a DriftResult and returns the exact Terraform command an engineer
should run to reconcile the drift.

Advisory only. The tool never executes any Terraform command.
The engineer copies the hint and runs it themselves.

Called after detect_drift() to populate the remediation field on each
DriftResult before the result is rendered or saved.
"""

from __future__ import annotations

from driftctl.models import DriftResult


def generate_remediation(result: DriftResult) -> str | None:
    """
    Return the advisory Terraform command for a drift result.

    Args:
        result: A DriftResult from the drift engine.

    Returns:
        A string containing the Terraform command(s) to fix the drift,
        or None for IN_SYNC results (no action needed).
    """
    if result.status == "IN_SYNC":
        return None

    if result.status == "MISSING":
        return _remediate_missing(result)

    if result.status == "UNMANAGED":
        return _remediate_unmanaged(result)

    if result.status == "MODIFIED":
        return _remediate_modified(result)

    if result.status == "TAG_DRIFT":
        return _remediate_tag_drift(result)

    return None


def enrich_results(results: list[DriftResult]) -> list[DriftResult]:
    """
    Populate the remediation field on every DriftResult in a list.
    Mutates in place and also returns the list for chaining.
    """
    for result in results:
        result.remediation = generate_remediation(result)
    return results


# ---------------------------------------------------------------------------
# Per-status remediation builders
# ---------------------------------------------------------------------------

def _remediate_missing(result: DriftResult) -> str:
    """
    MISSING: Resource is in Terraform state but gone from AWS.
    Two possible intentions — recreate or remove from state.
    """
    addr = _tf_address(result)
    return (
        f"# Option A — recreate (if deletion was unintentional):\n"
        f"terraform apply\n"
        f"\n"
        f"# Option B — remove from state (if deletion was intentional):\n"
        f"terraform state rm '{addr}'"
    )


def _remediate_unmanaged(result: DriftResult) -> str:
    """
    UNMANAGED: Resource exists in AWS but Terraform doesn't know about it.
    Import it into state, then add the matching HCL block.
    """
    suggested_name = _suggested_name(result)
    tf_type = result.type
    resource_id = result.id

    return (
        f"# Bring this resource under Terraform management:\n"
        f"terraform import {tf_type}.{suggested_name} {resource_id}\n"
        f"\n"
        f"# Then add the corresponding HCL resource block to your .tf files:\n"
        f'# resource "{tf_type}" "{suggested_name}" {{\n'
        f"#   # ... fill in the attributes\n"
        f"# }}"
    )


def _remediate_modified(result: DriftResult) -> str:
    """
    MODIFIED: One or more attributes differ between state and live AWS.
    Revert live resource back to the Terraform-declared values.
    """
    lines = [
        "# Revert live resource to its declared state:",
        "terraform apply",
        "#",
        "# The following attributes will be changed:",
    ]

    for field, diff in result.attribute_diffs.items():
        exp = _format_value(diff["expected"])
        act = _format_value(diff["actual"])
        lines.append(f"#   {field}: {act} → {exp}")

    return "\n".join(lines)


def _remediate_tag_drift(result: DriftResult) -> str:
    """
    TAG_DRIFT: Only tags differ. Resyncing tags is also done via apply.
    """
    lines = [
        "# Resync tags to their declared values:",
        "terraform apply",
        "#",
        "# The following tags will be updated:",
    ]

    for tag_key, diff in result.tag_diffs.items():
        exp = _format_value(diff["expected"])
        act = _format_value(diff["actual"])
        if diff["expected"] is None:
            lines.append(f"#   {tag_key}: remove (currently {act})")
        elif diff["actual"] is None:
            lines.append(f"#   {tag_key}: add with value {exp}")
        else:
            lines.append(f"#   {tag_key}: {act} → {exp}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tf_address(result: DriftResult) -> str:
    """
    Build the Terraform resource address: type.name
    Uses the logical name from state if available, otherwise the resource id.

    e.g. "aws_instance.web_server" or "aws_instance.i-0abc1234"
    """
    name = result.name or _suggested_name(result)
    return f"{result.type}.{name}"


def _suggested_name(result: DriftResult) -> str:
    """
    Derive a suggested Terraform logical name from the resource ID.
    Used for UNMANAGED resources that have no logical name yet.

    Examples:
      i-0abc1234567890def  → instance_i_0abc1234567890def
      vpc-0a1b2c3d4e5f6789 → vpc_0a1b2c3d4e5f6789
      sg-0111aaa222bbb333c → sg_0111aaa222bbb333c
      subnet-0a1b2c3d      → subnet_0a1b2c3d
      my-prod-bucket       → my_prod_bucket   (S3 uses bucket name as id)
    """
    resource_id = result.id

    # For S3 buckets the id is the bucket name — just replace hyphens
    if result.type == "aws_s3_bucket":
        return resource_id.replace("-", "_").replace(".", "_")

    # For EC2 resources: strip the AWS prefix (i-, vpc-, sg-, subnet-)
    # and prepend a human-readable prefix
    prefix_map = {
        "aws_instance":       "instance_",
        "aws_vpc":            "vpc_",
        "aws_subnet":         "subnet_",
        "aws_security_group": "sg_",
    }
    readable_prefix = prefix_map.get(result.type, "")

    # Strip the AWS-generated prefix from the id (e.g. "i-", "vpc-")
    clean_id = resource_id.replace("-", "_")
    return f"{readable_prefix}{clean_id}"


def _format_value(value: object) -> str:
    """
    Format a value for display in a remediation hint comment.
    Keeps it concise — long lists are truncated.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        if len(value) == 0:
            return "[]"
        if len(value) > 3:
            return f"[{value[0]!r}, ... ({len(value)} items)]"
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)
