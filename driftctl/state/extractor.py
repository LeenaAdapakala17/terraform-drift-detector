"""
driftctl/state/extractor.py

Converts raw tfstate resource records (from the state reader) into
normalised Resource dataclasses.

Each resource type has its own handler that maps tfstate field names
(snake_case) to the canonical attribute contract defined in SPEC Section 7.4.

If a resource type is not supported, returns None and logs a warning.
Unknown types never crash the scan.
"""

from __future__ import annotations

import logging
from typing import Callable

from driftctl.models import Resource, SGRule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_from_state(
    resource_type: str,
    resource_name: str,
    attributes: dict,
) -> Resource | None:
    """
    Convert a raw tfstate attribute block into a normalised Resource.

    Args:
        resource_type: e.g. "aws_instance"
        resource_name: terraform logical name e.g. "web_server"
        attributes:    raw dict from tfstate instances[].attributes

    Returns:
        Resource with source="expected", or None if type is unsupported.
    """
    handler = _DISPATCH.get(resource_type)
    if handler is None:
        logger.warning(
            "Unsupported resource type in state: %s.%s — skipping",
            resource_type, resource_name,
        )
        return None

    try:
        normalised_attrs, tags = handler(attributes)
    except Exception as exc:                        # pragma: no cover
        logger.error(
            "Failed to extract %s.%s from state: %s",
            resource_type, resource_name, exc,
        )
        return None

    resource_id = attributes.get("id", "")
    return Resource(
        type=resource_type,
        id=resource_id,
        name=resource_name,
        attributes=normalised_attrs,
        tags=tags,
        source="expected",
    )


# ---------------------------------------------------------------------------
# Per-type handlers
# Each returns (normalised_attributes: dict, tags: dict[str, str])
# ---------------------------------------------------------------------------

def _extract_instance(attrs: dict) -> tuple[dict, dict]:
    """aws_instance — EC2 instance."""
    # vpc_security_group_ids may be a list or a set-encoded value in tfstate
    sg_ids = attrs.get("vpc_security_group_ids")
    if isinstance(sg_ids, (list, set)):
        sg_ids_sorted = sorted(str(s) for s in sg_ids)
    else:
        sg_ids_sorted = []

    # root_block_device is a list of dicts in tfstate v4
    root_devices = attrs.get("root_block_device") or [{}]
    root = root_devices[0] if root_devices else {}

    normalised = {
        "instance_type":                attrs.get("instance_type", ""),
        "ami":                          attrs.get("ami", ""),
        "subnet_id":                    attrs.get("subnet_id") or "",
        "key_name":                     attrs.get("key_name"),
        "associate_public_ip_address":  _to_bool(attrs.get("associate_public_ip_address", False)),
        "monitoring":                   _to_bool(attrs.get("monitoring", False)),
        "iam_instance_profile":         _profile_name(attrs.get("iam_instance_profile")),
        "vpc_security_group_ids":       sg_ids_sorted,
        "ebs_optimized":                _to_bool(attrs.get("ebs_optimized", False)),
        "root_block_device_size":       int(root.get("volume_size") or 0),
        "root_block_device_type":       str(root.get("volume_type") or "gp2"),
    }
    tags = _extract_tags(attrs)
    return normalised, tags


def _extract_vpc(attrs: dict) -> tuple[dict, dict]:
    """aws_vpc — Virtual Private Cloud."""
    normalised = {
        "cidr_block":            attrs.get("cidr_block", ""),
        "instance_tenancy":      attrs.get("instance_tenancy", "default"),
        "enable_dns_support":    _to_bool(attrs.get("enable_dns_support", True)),
        "enable_dns_hostnames":  _to_bool(attrs.get("enable_dns_hostnames", False)),
    }
    tags = _extract_tags(attrs)
    return normalised, tags


def _extract_subnet(attrs: dict) -> tuple[dict, dict]:
    """aws_subnet — Subnet inside a VPC."""
    normalised = {
        "vpc_id":                   attrs.get("vpc_id", ""),
        "cidr_block":               attrs.get("cidr_block", ""),
        "availability_zone":        attrs.get("availability_zone", ""),
        "map_public_ip_on_launch":  _to_bool(attrs.get("map_public_ip_on_launch", False)),
    }
    tags = _extract_tags(attrs)
    return normalised, tags


def _extract_security_group(attrs: dict) -> tuple[dict, dict]:
    """aws_security_group — EC2 Security Group."""
    normalised = {
        "name":          attrs.get("name", ""),
        "description":   attrs.get("description", ""),
        "vpc_id":        attrs.get("vpc_id", ""),
        "ingress_rules": _normalise_sg_rules(attrs.get("ingress", []) or []),
        "egress_rules":  _normalise_sg_rules(attrs.get("egress", []) or []),
    }
    tags = _extract_tags(attrs)
    return normalised, tags


def _extract_s3_bucket(attrs: dict) -> tuple[dict, dict]:
    """
    aws_s3_bucket — S3 Bucket.

    In tfstate, S3 bucket configuration may be embedded as nested blocks
    (versioning, server_side_encryption_configuration, etc.) depending on
    the provider version used. We handle both the flat attribute style
    (older) and nested block style (newer).
    """
    # Versioning
    versioning = attrs.get("versioning") or [{}]
    versioning_block = versioning[0] if isinstance(versioning, list) and versioning else {}
    versioning_enabled = _to_bool(versioning_block.get("enabled", False))

    # Server-side encryption
    sse_config = attrs.get("server_side_encryption_configuration") or []
    sse_algorithm = None
    if sse_config and isinstance(sse_config, list):
        rules = sse_config[0].get("rule") or []
        if rules and isinstance(rules, list):
            apply_block = rules[0].get("apply_server_side_encryption_by_default") or [{}]
            if isinstance(apply_block, list) and apply_block:
                sse_algorithm = apply_block[0].get("sse_algorithm")
            elif isinstance(apply_block, dict):
                sse_algorithm = apply_block.get("sse_algorithm")

    # Public access block (may be a separate resource in newer configs,
    # but tfstate sometimes embeds it)
    pab = {}
    pab_list = attrs.get("public_access_block") or []
    if isinstance(pab_list, list) and pab_list:
        pab = pab_list[0]

    # Logging
    logging_block_list = attrs.get("logging") or []
    logging_block = {}
    if isinstance(logging_block_list, list) and logging_block_list:
        logging_block = logging_block_list[0]

    normalised = {
        "versioning_enabled":       versioning_enabled,
        "sse_algorithm":            sse_algorithm,
        "block_public_acls":        _to_bool(pab.get("block_public_acls", False)),
        "ignore_public_acls":       _to_bool(pab.get("ignore_public_acls", False)),
        "block_public_policy":      _to_bool(pab.get("block_public_policy", False)),
        "restrict_public_buckets":  _to_bool(pab.get("restrict_public_buckets", False)),
        "logging_target_bucket":    logging_block.get("target_bucket"),
        "logging_target_prefix":    logging_block.get("target_prefix"),
    }
    tags = _extract_tags(attrs)
    return normalised, tags


# ---------------------------------------------------------------------------
# Dispatch table — maps resource type string → handler function
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Callable[[dict], tuple[dict, dict]]] = {
    "aws_instance":       _extract_instance,
    "aws_vpc":            _extract_vpc,
    "aws_subnet":         _extract_subnet,
    "aws_security_group": _extract_security_group,
    "aws_s3_bucket":      _extract_s3_bucket,
}


def supported_types() -> list[str]:
    """Return the list of resource types the state extractor handles."""
    return list(_DISPATCH.keys())


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _to_bool(value: object) -> bool:
    """
    Coerce a tfstate value to bool.
    tfstate can store booleans as Python bools, or as the strings "true"/"false".
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _extract_tags(attrs: dict) -> dict[str, str]:
    """
    Extract the tags dict from a tfstate attributes block.
    In tfstate v4, tags are stored as {"key": "value"} dict directly.
    Returns an empty dict if absent or None.
    """
    raw = attrs.get("tags") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k and v is not None}


def _profile_name(value: object) -> str | None:
    """
    Normalise IAM instance profile to its name (not ARN).
    tfstate may store the profile name directly or as a nested structure.
    """
    if not value:
        return None
    if isinstance(value, str):
        # Strip ARN prefix if present: arn:aws:iam::123456:instance-profile/MyProfile
        if "instance-profile/" in value:
            return value.split("instance-profile/")[-1]
        return value
    if isinstance(value, list) and value:
        return _profile_name(value[0])
    if isinstance(value, dict):
        return value.get("name") or value.get("arn")
    return str(value)


def _normalise_sg_rules(rules: list[dict]) -> list[SGRule]:
    """
    Convert a list of raw tfstate security group rule dicts into
    a sorted list of SGRule dataclasses.

    Sorting ensures that ordering differences between tfstate and the
    AWS API response never produce false drift.
    """
    result = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue

        protocol  = str(rule.get("protocol", "-1"))
        from_port = int(rule.get("from_port") or 0)
        to_port   = int(rule.get("to_port") or 0)

        cidr_blocks      = tuple(sorted(rule.get("cidr_blocks") or []))
        ipv6_cidr_blocks = tuple(sorted(rule.get("ipv6_cidr_blocks") or []))

        # Source SG IDs may come from "security_groups" key in tfstate
        source_sg_ids = tuple(sorted(rule.get("security_groups") or []))

        result.append(SGRule(
            protocol=protocol,
            from_port=from_port,
            to_port=to_port,
            cidr_blocks=cidr_blocks,
            ipv6_cidr_blocks=ipv6_cidr_blocks,
            source_sg_ids=source_sg_ids,
        ))

    # Sort by (protocol, from_port, to_port) so rule order never matters
    return sorted(result)
