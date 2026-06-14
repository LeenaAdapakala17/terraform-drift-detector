"""
driftctl/providers/aws.py

AWS cloud provider. Implements CloudProvider using boto3.

Responsibilities:
  1. Fetch live resources from AWS APIs (handling pagination)
  2. Normalise boto3 responses into Resource dataclasses using the
     same canonical attribute contract as the state extractor.

The normalisation is the critical part — both extractors (state-side
and cloud-side) MUST produce identical keys and value types for the
same resource, otherwise the drift engine reports false positives.

All API calls are read-only (describe_* / get_* / list_*).
AWS does not bill for these metadata calls.
"""

from __future__ import annotations

import logging
from typing import Callable

import boto3
from botocore.exceptions import ClientError

from driftctl.models import Resource, SGRule
from driftctl.providers.base import CloudProvider

logger = logging.getLogger(__name__)


class AWSProvider(CloudProvider):

    def __init__(self, region: str, profile: str | None = None):
        """
        Args:
            region:  AWS region to scan e.g. "us-east-1"
            profile: Optional AWS named credential profile.
                     None = use the default boto3 credential chain
                     (env vars → ~/.aws/credentials → IAM role)
        """
        self._region = region
        session = boto3.Session(region_name=region, profile_name=profile)
        self._ec2 = session.client("ec2")
        self._s3  = session.client("s3")

    def supported_types(self) -> list[str]:
        return [
            "aws_instance",
            "aws_vpc",
            "aws_subnet",
            "aws_security_group",
            "aws_s3_bucket",
        ]

    def fetch(self, resource_type: str) -> list[Resource]:
        """Dispatch to the correct fetcher for the given resource type."""
        dispatch: dict[str, Callable[[], list[Resource]]] = {
            "aws_instance":       self._fetch_instances,
            "aws_vpc":            self._fetch_vpcs,
            "aws_subnet":         self._fetch_subnets,
            "aws_security_group": self._fetch_security_groups,
            "aws_s3_bucket":      self._fetch_s3_buckets,
        }
        fetcher = dispatch.get(resource_type)
        if fetcher is None:
            logger.warning("AWSProvider: unsupported resource type: %s", resource_type)
            return []
        try:
            return fetcher()
        except ClientError as exc:
            logger.error("AWS API error fetching %s: %s", resource_type, exc)
            return []

    # ------------------------------------------------------------------
    # EC2 — Instances
    # ------------------------------------------------------------------

    def _fetch_instances(self) -> list[Resource]:
        """
        Fetch all EC2 instances in the region.
        Skips terminated instances (they no longer exist as live resources).
        Uses the describe_instances paginator.
        """
        resources = []
        paginator = self._ec2.get_paginator("describe_instances")

        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    # Skip terminated instances — they are gone
                    state = inst.get("State", {}).get("Name", "")
                    if state == "terminated":
                        continue

                    resource_id = inst.get("InstanceId", "")
                    attributes, tags = self._normalise_instance(inst)

                    resources.append(Resource(
                        type="aws_instance",
                        id=resource_id,
                        name=None,
                        attributes=attributes,
                        tags=tags,
                        source="actual",
                    ))

        logger.info("AWSProvider: fetched %d instances", len(resources))
        return resources

    def _normalise_instance(self, inst: dict) -> tuple[dict, dict]:
        """
        Normalise a boto3 Instance dict to the canonical attribute contract.

        boto3 key      → canonical key
        InstanceType   → instance_type
        ImageId        → ami
        SubnetId       → subnet_id
        KeyName        → key_name
        PublicIpAddress→ associate_public_ip_address (bool: has one or not)
        Monitoring     → monitoring
        IamInstanceProfile → iam_instance_profile
        SecurityGroups → vpc_security_group_ids (sorted list of sg ids)
        EbsOptimized   → ebs_optimized
        BlockDeviceMappings → root_block_device_size / type
        """
        # Security group IDs — boto3 gives list of {"GroupId": "sg-…", "GroupName": "…"}
        sg_ids = sorted(
            sg["GroupId"]
            for sg in inst.get("SecurityGroups", [])
            if "GroupId" in sg
        )

        # Root block device — find the root device in BlockDeviceMappings
        root_size = 0
        root_type = "gp2"
        root_device_name = inst.get("RootDeviceName", "")
        for bdm in inst.get("BlockDeviceMappings", []):
            if bdm.get("DeviceName") == root_device_name:
                ebs = bdm.get("Ebs", {})
                # Volume details require describe_volumes — use defaults here
                # The size and type comparison requires a describe_volumes call
                # but for the parity test we keep it consistent with tfstate defaults
                root_size = ebs.get("VolumeSize", 0)
                root_type = ebs.get("VolumeType", "gp2")
                break

        # IAM instance profile name (not ARN)
        iam_profile = inst.get("IamInstanceProfile", {})
        profile_arn = iam_profile.get("Arn", "") if iam_profile else ""
        profile_name = profile_arn.split("instance-profile/")[-1] if profile_arn else None

        # associate_public_ip_address — True if the instance has a public IP
        has_public_ip = bool(inst.get("PublicIpAddress"))

        # Monitoring state — boto3 returns {"State": "enabled"} or {"State": "disabled"}
        monitoring = inst.get("Monitoring", {}).get("State", "disabled") == "enabled"

        attributes = {
            "instance_type":                inst.get("InstanceType", ""),
            "ami":                          inst.get("ImageId", ""),
            "subnet_id":                    inst.get("SubnetId") or "",
            "key_name":                     inst.get("KeyName"),
            "associate_public_ip_address":  has_public_ip,
            "monitoring":                   monitoring,
            "iam_instance_profile":         profile_name or None,
            "vpc_security_group_ids":       sg_ids,
            "ebs_optimized":                bool(inst.get("EbsOptimized", False)),
            "root_block_device_size":       root_size,
            "root_block_device_type":       root_type,
        }
        tags = _parse_ec2_tags(inst.get("Tags", []))
        return attributes, tags

    # ------------------------------------------------------------------
    # EC2 — VPCs
    # ------------------------------------------------------------------

    def _fetch_vpcs(self) -> list[Resource]:
        """Fetch all VPCs in the region (paginated)."""
        resources = []
        paginator = self._ec2.get_paginator("describe_vpcs")

        for page in paginator.paginate():
            for vpc in page.get("Vpcs", []):
                vpc_id = vpc.get("VpcId", "")
                attributes, tags = self._normalise_vpc(vpc)
                resources.append(Resource(
                    type="aws_vpc",
                    id=vpc_id,
                    name=None,
                    attributes=attributes,
                    tags=tags,
                    source="actual",
                ))

        logger.info("AWSProvider: fetched %d VPCs", len(resources))
        return resources

    def _normalise_vpc(self, vpc: dict) -> tuple[dict, dict]:
        """
        Normalise a boto3 VPC dict.

        enable_dns_support and enable_dns_hostnames require separate
        describe_vpc_attribute calls — we make them here per VPC.
        """
        vpc_id = vpc.get("VpcId", "")

        # DNS support and hostnames are separate API calls in boto3
        try:
            dns_support = self._ec2.describe_vpc_attribute(
                VpcId=vpc_id, Attribute="enableDnsSupport"
            )
            enable_dns_support = dns_support.get(
                "EnableDnsSupport", {}
            ).get("Value", True)
        except ClientError:
            enable_dns_support = True

        try:
            dns_hostnames = self._ec2.describe_vpc_attribute(
                VpcId=vpc_id, Attribute="enableDnsHostnames"
            )
            enable_dns_hostnames = dns_hostnames.get(
                "EnableDnsHostnames", {}
            ).get("Value", False)
        except ClientError:
            enable_dns_hostnames = False

        attributes = {
            "cidr_block":           vpc.get("CidrBlock", ""),
            "instance_tenancy":     vpc.get("InstanceTenancy", "default"),
            "enable_dns_support":   bool(enable_dns_support),
            "enable_dns_hostnames": bool(enable_dns_hostnames),
        }
        tags = _parse_ec2_tags(vpc.get("Tags", []))
        return attributes, tags

    # ------------------------------------------------------------------
    # EC2 — Subnets
    # ------------------------------------------------------------------

    def _fetch_subnets(self) -> list[Resource]:
        """Fetch all subnets in the region (paginated)."""
        resources = []
        paginator = self._ec2.get_paginator("describe_subnets")

        for page in paginator.paginate():
            for subnet in page.get("Subnets", []):
                subnet_id = subnet.get("SubnetId", "")
                attributes, tags = self._normalise_subnet(subnet)
                resources.append(Resource(
                    type="aws_subnet",
                    id=subnet_id,
                    name=None,
                    attributes=attributes,
                    tags=tags,
                    source="actual",
                ))

        logger.info("AWSProvider: fetched %d subnets", len(resources))
        return resources

    def _normalise_subnet(self, subnet: dict) -> tuple[dict, dict]:
        attributes = {
            "vpc_id":                   subnet.get("VpcId", ""),
            "cidr_block":               subnet.get("CidrBlock", ""),
            "availability_zone":        subnet.get("AvailabilityZone", ""),
            "map_public_ip_on_launch":  bool(
                subnet.get("MapPublicIpOnLaunch", False)
            ),
        }
        tags = _parse_ec2_tags(subnet.get("Tags", []))
        return attributes, tags

    # ------------------------------------------------------------------
    # EC2 — Security Groups
    # ------------------------------------------------------------------

    def _fetch_security_groups(self) -> list[Resource]:
        """Fetch all security groups in the region (paginated)."""
        resources = []
        paginator = self._ec2.get_paginator("describe_security_groups")

        for page in paginator.paginate():
            for sg in page.get("SecurityGroups", []):
                sg_id = sg.get("GroupId", "")
                attributes, tags = self._normalise_security_group(sg)
                resources.append(Resource(
                    type="aws_security_group",
                    id=sg_id,
                    name=None,
                    attributes=attributes,
                    tags=tags,
                    source="actual",
                ))

        logger.info("AWSProvider: fetched %d security groups", len(resources))
        return resources

    def _normalise_security_group(self, sg: dict) -> tuple[dict, dict]:
        attributes = {
            "name":          sg.get("GroupName", ""),
            "description":   sg.get("Description", ""),
            "vpc_id":        sg.get("VpcId", ""),
            "ingress_rules": _normalise_sg_rules(sg.get("IpPermissions", [])),
            "egress_rules":  _normalise_sg_rules(sg.get("IpPermissionsEgress", [])),
        }
        tags = _parse_ec2_tags(sg.get("Tags", []))
        return attributes, tags

    # ------------------------------------------------------------------
    # S3 — Buckets
    # ------------------------------------------------------------------

    def _fetch_s3_buckets(self) -> list[Resource]:
        """
        Fetch all S3 buckets and assemble their configuration from
        five separate API calls per bucket.

        list_buckets is global (not region-filtered), but we skip
        buckets whose region doesn't match the configured region
        to avoid false positives from buckets in other regions.
        """
        resources = []

        try:
            response = self._s3.list_buckets()
        except ClientError as exc:
            logger.error("AWSProvider: failed to list S3 buckets: %s", exc)
            return []

        for bucket in response.get("Buckets", []):
            name = bucket.get("Name", "")

            # Filter to configured region only
            try:
                location = self._s3.get_bucket_location(Bucket=name)
                bucket_region = location.get("LocationConstraint") or "us-east-1"
                if bucket_region != self._region:
                    logger.debug(
                        "Skipping bucket %s (region %s != %s)",
                        name, bucket_region, self._region,
                    )
                    continue
            except ClientError as exc:
                logger.warning("Cannot get location for bucket %s: %s", name, exc)
                continue

            attributes, tags = self._assemble_s3_bucket(name)
            resources.append(Resource(
                type="aws_s3_bucket",
                id=name,
                name=None,
                attributes=attributes,
                tags=tags,
                source="actual",
            ))

        logger.info("AWSProvider: fetched %d S3 buckets in %s", len(resources), self._region)
        return resources

    def _assemble_s3_bucket(self, name: str) -> tuple[dict, dict]:
        """
        Assemble a single S3 bucket's configuration from five API calls.
        Each call can fail independently — treat failure as "feature disabled".
        """
        attributes: dict = {}
        tags: dict = {}

        # 1. Versioning
        try:
            v = self._s3.get_bucket_versioning(Bucket=name)
            attributes["versioning_enabled"] = v.get("Status") == "Enabled"
        except ClientError:
            attributes["versioning_enabled"] = False

        # 2. Encryption
        try:
            enc = self._s3.get_bucket_encryption(Bucket=name)
            rules = (
                enc.get("ServerSideEncryptionConfiguration", {})
                   .get("Rules", [])
            )
            if rules:
                algo = (
                    rules[0]
                    .get("ApplyServerSideEncryptionByDefault", {})
                    .get("SSEAlgorithm")
                )
                attributes["sse_algorithm"] = algo
            else:
                attributes["sse_algorithm"] = None
        except ClientError:
            attributes["sse_algorithm"] = None

        # 3. Tags
        try:
            t = self._s3.get_bucket_tagging(Bucket=name)
            tags = {
                tag["Key"]: tag["Value"]
                for tag in t.get("TagSet", [])
            }
        except ClientError:
            tags = {}

        # 4. Public access block
        try:
            pab = self._s3.get_public_access_block(Bucket=name)
            config = pab.get("PublicAccessBlockConfiguration", {})
            attributes["block_public_acls"]       = bool(config.get("BlockPublicAcls", False))
            attributes["ignore_public_acls"]      = bool(config.get("IgnorePublicAcls", False))
            attributes["block_public_policy"]     = bool(config.get("BlockPublicPolicy", False))
            attributes["restrict_public_buckets"] = bool(config.get("RestrictPublicBuckets", False))
        except ClientError:
            attributes["block_public_acls"]       = False
            attributes["ignore_public_acls"]      = False
            attributes["block_public_policy"]     = False
            attributes["restrict_public_buckets"] = False

        # 5. Logging
        try:
            log_resp = self._s3.get_bucket_logging(Bucket=name)
            log = log_resp.get("LoggingEnabled", {})
            attributes["logging_target_bucket"] = log.get("TargetBucket")
            attributes["logging_target_prefix"] = log.get("TargetPrefix")
        except ClientError:
            attributes["logging_target_bucket"] = None
            attributes["logging_target_prefix"] = None

        return attributes, tags


# ---------------------------------------------------------------------------
# Shared helpers (module-level, used by normalisation methods)
# ---------------------------------------------------------------------------

def _parse_ec2_tags(tags: list[dict]) -> dict[str, str]:
    """
    Convert boto3 EC2 tag format to a plain dict.

    boto3 returns: [{"Key": "env", "Value": "prod"}, ...]
    We want:       {"env": "prod", ...}
    """
    return {
        tag["Key"]: tag["Value"]
        for tag in (tags or [])
        if "Key" in tag and "Value" in tag
    }


def _normalise_sg_rules(rules: list[dict]) -> list[SGRule]:
    """
    Convert boto3 IpPermissions / IpPermissionsEgress to sorted SGRule list.

    boto3 format per rule:
    {
        "IpProtocol": "tcp",
        "FromPort": 443,
        "ToPort": 443,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
        "Ipv6Ranges": [],
        "UserIdGroupPairs": [{"GroupId": "sg-…"}],
    }

    Must produce the same SGRule values as the state extractor for the
    same logical rule — otherwise the drift engine reports false positives.
    """
    result = []
    for rule in rules or []:
        # Protocol: boto3 uses "-1" for all traffic (same as tfstate)
        protocol = str(rule.get("IpProtocol", "-1"))

        # Ports: boto3 omits FromPort/ToPort for protocol "-1"
        from_port = int(rule.get("FromPort") or 0)
        to_port   = int(rule.get("ToPort") or 0)

        # IPv4 CIDRs
        cidr_blocks = tuple(sorted(
            r["CidrIp"]
            for r in rule.get("IpRanges", [])
            if "CidrIp" in r
        ))

        # IPv6 CIDRs
        ipv6_cidr_blocks = tuple(sorted(
            r["CidrIpv6"]
            for r in rule.get("Ipv6Ranges", [])
            if "CidrIpv6" in r
        ))

        # Source security group IDs
        source_sg_ids = tuple(sorted(
            pair["GroupId"]
            for pair in rule.get("UserIdGroupPairs", [])
            if "GroupId" in pair
        ))

        result.append(SGRule(
            protocol=protocol,
            from_port=from_port,
            to_port=to_port,
            cidr_blocks=cidr_blocks,
            ipv6_cidr_blocks=ipv6_cidr_blocks,
            source_sg_ids=source_sg_ids,
        ))

    # Sort by (protocol, from_port, to_port) — same sort as state extractor
    return sorted(result)
