"""
tests/test_aws_provider.py

Unit tests for driftctl/providers/aws.py

All AWS API calls are mocked with moto — no real AWS credentials needed.
The suite runs fully offline.

Key test categories:
  1. Basic fetch tests — create a resource in moto, fetch it, assert shape
  2. Parity tests — the CRITICAL test: same resource through both extractors
     must produce identical normalised output so the drift engine sees IN_SYNC
  3. Edge cases — empty region, terminated instances, missing S3 features
  4. Tag normalisation — boto3 [{"Key":…,"Value":…}] → {"key": "value"}
  5. SG rule normalisation — sorting, protocol -1, multiple CIDRs
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from driftctl.models import Resource, SGRule
from driftctl.providers.aws import AWSProvider
from driftctl.state.extractor import extract_from_state

REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def aws_provider():
    return AWSProvider(region=REGION)


@pytest.fixture
def ec2_client():
    return boto3.client("ec2", region_name=REGION)


@pytest.fixture
def s3_client():
    return boto3.client("s3", region_name=REGION)


# ---------------------------------------------------------------------------
# Helper — create a VPC (needed for most EC2 resources)
# ---------------------------------------------------------------------------

def _create_vpc(ec2) -> str:
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")
    return vpc["Vpc"]["VpcId"]


# ---------------------------------------------------------------------------
# aws_instance
# ---------------------------------------------------------------------------

@mock_aws
class TestFetchInstances:

    def test_fetch_returns_list(self, aws_provider, ec2_client):
        """fetch() should return a list even when no instances exist."""
        result = aws_provider.fetch("aws_instance")
        assert isinstance(result, list)

    def test_fetch_single_instance(self, aws_provider, ec2_client):
        """Create one instance, fetch it, check type and source."""
        ec2_client.run_instances(
            ImageId="ami-0c55b159cbfafe1f0",
            MinCount=1,
            MaxCount=1,
            InstanceType="t2.micro",
        )
        results = aws_provider.fetch("aws_instance")
        assert len(results) == 1
        r = results[0]
        assert r.type == "aws_instance"
        assert r.source == "actual"
        assert r.id.startswith("i-")

    def test_instance_attributes_shape(self, aws_provider, ec2_client):
        """Fetched instance should have all canonical attribute keys."""
        ec2_client.run_instances(
            ImageId="ami-0c55b159cbfafe1f0",
            MinCount=1, MaxCount=1,
            InstanceType="t2.micro",
        )
        r = aws_provider.fetch("aws_instance")[0]
        expected_keys = {
            "instance_type", "ami", "subnet_id", "key_name",
            "associate_public_ip_address", "monitoring",
            "iam_instance_profile", "vpc_security_group_ids",
            "ebs_optimized", "root_block_device_size", "root_block_device_type",
        }
        assert expected_keys.issubset(set(r.attributes.keys()))

    def test_instance_type_value(self, aws_provider, ec2_client):
        ec2_client.run_instances(
            ImageId="ami-0c55b159cbfafe1f0",
            MinCount=1, MaxCount=1,
            InstanceType="t2.micro",
        )
        r = aws_provider.fetch("aws_instance")[0]
        assert r.attributes["instance_type"] == "t2.micro"

    def test_terminated_instances_skipped(self, aws_provider, ec2_client):
        """Terminated instances must not appear in fetch results."""
        resp = ec2_client.run_instances(
            ImageId="ami-0c55b159cbfafe1f0",
            MinCount=1, MaxCount=1,
            InstanceType="t2.micro",
        )
        instance_id = resp["Instances"][0]["InstanceId"]
        ec2_client.terminate_instances(InstanceIds=[instance_id])

        results = aws_provider.fetch("aws_instance")
        ids = [r.id for r in results]
        assert instance_id not in ids

    def test_instance_tags(self, aws_provider, ec2_client):
        """Tags should be normalised from boto3 list format to dict."""
        resp = ec2_client.run_instances(
            ImageId="ami-0c55b159cbfafe1f0",
            MinCount=1, MaxCount=1,
            InstanceType="t2.micro",
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "env", "Value": "production"},
                    {"Key": "Owner", "Value": "platform-team"},
                ],
            }],
        )
        results = aws_provider.fetch("aws_instance")
        r = results[0]
        assert r.tags["env"] == "production"
        assert r.tags["Owner"] == "platform-team"

    def test_sg_ids_are_sorted(self, aws_provider, ec2_client):
        """vpc_security_group_ids must be sorted."""
        vpc_id = _create_vpc(ec2_client)
        sg1 = ec2_client.create_security_group(
            GroupName="sg-zzz", Description="z", VpcId=vpc_id
        )["GroupId"]
        sg2 = ec2_client.create_security_group(
            GroupName="sg-aaa", Description="a", VpcId=vpc_id
        )["GroupId"]

        ec2_client.run_instances(
            ImageId="ami-0c55b159cbfafe1f0",
            MinCount=1, MaxCount=1,
            InstanceType="t2.micro",
            SecurityGroupIds=[sg1, sg2],
        )
        r = aws_provider.fetch("aws_instance")[0]
        sg_ids = r.attributes["vpc_security_group_ids"]
        assert sg_ids == sorted(sg_ids)


# ---------------------------------------------------------------------------
# aws_vpc
# ---------------------------------------------------------------------------

@mock_aws
class TestFetchVpcs:

    def test_fetch_returns_list(self, aws_provider):
        result = aws_provider.fetch("aws_vpc")
        assert isinstance(result, list)

    def test_fetch_created_vpc(self, aws_provider, ec2_client):
        ec2_client.create_vpc(CidrBlock="10.0.0.0/16")
        results = aws_provider.fetch("aws_vpc")
        # moto also creates a default VPC, so filter to our CIDR
        our_vpcs = [r for r in results if r.attributes["cidr_block"] == "10.0.0.0/16"]
        assert len(our_vpcs) == 1
        r = our_vpcs[0]
        assert r.type == "aws_vpc"
        assert r.source == "actual"
        assert r.id.startswith("vpc-")

    def test_vpc_attributes_shape(self, aws_provider, ec2_client):
        ec2_client.create_vpc(CidrBlock="10.1.0.0/16")
        results = aws_provider.fetch("aws_vpc")
        our = [r for r in results if r.attributes["cidr_block"] == "10.1.0.0/16"][0]
        expected_keys = {
            "cidr_block", "instance_tenancy",
            "enable_dns_support", "enable_dns_hostnames",
        }
        assert expected_keys.issubset(set(our.attributes.keys()))

    def test_vpc_cidr_block(self, aws_provider, ec2_client):
        ec2_client.create_vpc(CidrBlock="172.16.0.0/16")
        results = aws_provider.fetch("aws_vpc")
        our = [r for r in results if r.attributes["cidr_block"] == "172.16.0.0/16"]
        assert len(our) == 1

    def test_vpc_tags(self, aws_provider, ec2_client):
        resp = ec2_client.create_vpc(CidrBlock="10.2.0.0/16")
        vpc_id = resp["Vpc"]["VpcId"]
        ec2_client.create_tags(
            Resources=[vpc_id],
            Tags=[{"Key": "Name", "Value": "test-vpc"}, {"Key": "env", "Value": "dev"}],
        )
        results = aws_provider.fetch("aws_vpc")
        our = [r for r in results if r.id == vpc_id][0]
        assert our.tags["Name"] == "test-vpc"
        assert our.tags["env"] == "dev"


# ---------------------------------------------------------------------------
# aws_subnet
# ---------------------------------------------------------------------------

@mock_aws
class TestFetchSubnets:

    def test_fetch_returns_list(self, aws_provider):
        result = aws_provider.fetch("aws_subnet")
        assert isinstance(result, list)

    def test_fetch_created_subnet(self, aws_provider, ec2_client):
        vpc_id = _create_vpc(ec2_client)
        ec2_client.create_subnet(
            VpcId=vpc_id,
            CidrBlock="10.0.1.0/24",
            AvailabilityZone=f"{REGION}a",
        )
        results = aws_provider.fetch("aws_subnet")
        our = [r for r in results if r.attributes["cidr_block"] == "10.0.1.0/24"]
        assert len(our) == 1
        r = our[0]
        assert r.type == "aws_subnet"
        assert r.source == "actual"
        assert r.id.startswith("subnet-")

    def test_subnet_attributes_shape(self, aws_provider, ec2_client):
        vpc_id = _create_vpc(ec2_client)
        ec2_client.create_subnet(VpcId=vpc_id, CidrBlock="10.0.2.0/24")
        results = aws_provider.fetch("aws_subnet")
        our = [r for r in results if r.attributes["cidr_block"] == "10.0.2.0/24"][0]
        expected_keys = {
            "vpc_id", "cidr_block",
            "availability_zone", "map_public_ip_on_launch",
        }
        assert expected_keys.issubset(set(our.attributes.keys()))

    def test_subnet_vpc_id(self, aws_provider, ec2_client):
        vpc_id = _create_vpc(ec2_client)
        ec2_client.create_subnet(VpcId=vpc_id, CidrBlock="10.0.3.0/24")
        results = aws_provider.fetch("aws_subnet")
        our = [r for r in results if r.attributes["cidr_block"] == "10.0.3.0/24"][0]
        assert our.attributes["vpc_id"] == vpc_id

    def test_subnet_map_public_ip_default_false(self, aws_provider, ec2_client):
        vpc_id = _create_vpc(ec2_client)
        ec2_client.create_subnet(VpcId=vpc_id, CidrBlock="10.0.4.0/24")
        results = aws_provider.fetch("aws_subnet")
        our = [r for r in results if r.attributes["cidr_block"] == "10.0.4.0/24"][0]
        assert our.attributes["map_public_ip_on_launch"] is False


# ---------------------------------------------------------------------------
# aws_security_group
# ---------------------------------------------------------------------------

@mock_aws
class TestFetchSecurityGroups:

    def test_fetch_returns_list(self, aws_provider):
        result = aws_provider.fetch("aws_security_group")
        assert isinstance(result, list)

    def test_fetch_created_sg(self, aws_provider, ec2_client):
        vpc_id = _create_vpc(ec2_client)
        ec2_client.create_security_group(
            GroupName="web-sg",
            Description="Web security group",
            VpcId=vpc_id,
        )
        results = aws_provider.fetch("aws_security_group")
        our = [r for r in results if r.attributes["name"] == "web-sg"]
        assert len(our) == 1
        r = our[0]
        assert r.type == "aws_security_group"
        assert r.source == "actual"
        assert r.id.startswith("sg-")

    def test_sg_attributes_shape(self, aws_provider, ec2_client):
        vpc_id = _create_vpc(ec2_client)
        ec2_client.create_security_group(
            GroupName="test-sg", Description="test", VpcId=vpc_id,
        )
        results = aws_provider.fetch("aws_security_group")
        our = [r for r in results if r.attributes["name"] == "test-sg"][0]
        expected_keys = {
            "name", "description", "vpc_id",
            "ingress_rules", "egress_rules",
        }
        assert expected_keys.issubset(set(our.attributes.keys()))

    def test_sg_ingress_rules_normalised(self, aws_provider, ec2_client):
        """Ingress rules should be sorted SGRule objects."""
        vpc_id = _create_vpc(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName="rules-sg", Description="rules", VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
            ],
        )
        results = aws_provider.fetch("aws_security_group")
        our = [r for r in results if r.id == sg_id][0]
        ingress = our.attributes["ingress_rules"]
        assert len(ingress) == 2
        assert all(isinstance(rule, SGRule) for rule in ingress)
        # Should be sorted: port 80 before port 443
        assert ingress[0].from_port == 80
        assert ingress[1].from_port == 443

    def test_sg_rules_cidr_sorted(self, aws_provider, ec2_client):
        """Multiple CIDRs within one rule must be sorted."""
        vpc_id = _create_vpc(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName="cidr-sg", Description="cidr", VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [
                    {"CidrIp": "192.168.0.0/16"},
                    {"CidrIp": "10.0.0.0/8"},
                ],
            }],
        )
        results = aws_provider.fetch("aws_security_group")
        our = [r for r in results if r.id == sg_id][0]
        rule = our.attributes["ingress_rules"][0]
        assert rule.cidr_blocks == tuple(sorted(["192.168.0.0/16", "10.0.0.0/8"]))


# ---------------------------------------------------------------------------
# aws_s3_bucket
# ---------------------------------------------------------------------------

@mock_aws
class TestFetchS3Buckets:

    def test_fetch_returns_list(self, aws_provider):
        result = aws_provider.fetch("aws_s3_bucket")
        assert isinstance(result, list)

    def test_fetch_created_bucket(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="my-test-bucket")
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "my-test-bucket"]
        assert len(our) == 1
        r = our[0]
        assert r.type == "aws_s3_bucket"
        assert r.source == "actual"

    def test_s3_attributes_shape(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="shape-test-bucket")
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "shape-test-bucket"][0]
        expected_keys = {
            "versioning_enabled", "sse_algorithm",
            "block_public_acls", "ignore_public_acls",
            "block_public_policy", "restrict_public_buckets",
            "logging_target_bucket", "logging_target_prefix",
        }
        assert expected_keys.issubset(set(our.attributes.keys()))

    def test_s3_versioning_enabled(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="versioned-bucket")
        s3_client.put_bucket_versioning(
            Bucket="versioned-bucket",
            VersioningConfiguration={"Status": "Enabled"},
        )
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "versioned-bucket"][0]
        assert our.attributes["versioning_enabled"] is True

    def test_s3_versioning_disabled_by_default(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="unversioned-bucket")
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "unversioned-bucket"][0]
        assert our.attributes["versioning_enabled"] is False

    def test_s3_tags(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="tagged-bucket")
        s3_client.put_bucket_tagging(
            Bucket="tagged-bucket",
            Tagging={"TagSet": [
                {"Key": "env", "Value": "production"},
                {"Key": "CostCenter", "Value": "engineering"},
            ]},
        )
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "tagged-bucket"][0]
        assert our.tags["env"] == "production"
        assert our.tags["CostCenter"] == "engineering"

    def test_s3_public_access_block(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="pab-bucket")
        s3_client.put_public_access_block(
            Bucket="pab-bucket",
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "pab-bucket"][0]
        assert our.attributes["block_public_acls"] is True
        assert our.attributes["ignore_public_acls"] is True
        assert our.attributes["block_public_policy"] is True
        assert our.attributes["restrict_public_buckets"] is True

    def test_s3_no_public_access_block_defaults_false(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="no-pab-bucket")
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "no-pab-bucket"][0]
        assert our.attributes["block_public_acls"] is False

    def test_s3_encryption(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="encrypted-bucket")
        s3_client.put_bucket_encryption(
            Bucket="encrypted-bucket",
            ServerSideEncryptionConfiguration={
                "Rules": [{
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256",
                    },
                }],
            },
        )
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "encrypted-bucket"][0]
        assert our.attributes["sse_algorithm"] == "AES256"

    def test_s3_no_encryption_is_none(self, aws_provider, s3_client):
        s3_client.create_bucket(Bucket="plain-bucket")
        results = aws_provider.fetch("aws_s3_bucket")
        our = [r for r in results if r.id == "plain-bucket"][0]
        assert our.attributes["sse_algorithm"] is None


# ---------------------------------------------------------------------------
# PARITY TESTS — The most important tests in Phase 2.
#
# For the same resource, the state extractor and cloud extractor MUST
# produce identical normalised attribute dicts and tag dicts.
# If they don't, the drift engine will report false positives on every
# scan, even when nothing has actually drifted.
#
# Pattern:
#   1. Build tfstate attributes for a resource
#   2. Create the same resource in the moto mock
#   3. Run both extractors
#   4. Assert attributes and tags are identical
# ---------------------------------------------------------------------------

@mock_aws
class TestParityStateVsCloud:
    """
    Parity tests: state extractor output == cloud extractor output
    for the same resource means drift engine produces IN_SYNC.
    """

    def test_vpc_parity(self, aws_provider, ec2_client):
        """VPC: both extractors should produce identical attributes."""
        resp = ec2_client.create_vpc(CidrBlock="10.0.0.0/16")
        vpc_id = resp["Vpc"]["VpcId"]

        # State-side: what tfstate would contain for this VPC
        state_attrs = {
            "id":                   vpc_id,
            "cidr_block":           "10.0.0.0/16",
            "instance_tenancy":     "default",
            "enable_dns_support":   True,
            "enable_dns_hostnames": False,
            "tags":                 {},
        }
        state_resource = extract_from_state("aws_vpc", "main", state_attrs)

        # Cloud-side: fetch from moto
        cloud_resources = aws_provider.fetch("aws_vpc")
        cloud_resource = next((r for r in cloud_resources if r.id == vpc_id), None)
        assert cloud_resource is not None

        # Parity check
        assert state_resource.attributes == cloud_resource.attributes, (
            f"VPC attribute mismatch:\n"
            f"  state:  {state_resource.attributes}\n"
            f"  cloud:  {cloud_resource.attributes}"
        )

    def test_subnet_parity(self, aws_provider, ec2_client):
        """Subnet: both extractors should produce identical attributes."""
        vpc_id = _create_vpc(ec2_client)
        resp = ec2_client.create_subnet(
            VpcId=vpc_id,
            CidrBlock="10.0.1.0/24",
            AvailabilityZone=f"{REGION}a",
        )
        subnet_id = resp["Subnet"]["SubnetId"]

        state_attrs = {
            "id":                      subnet_id,
            "vpc_id":                  vpc_id,
            "cidr_block":              "10.0.1.0/24",
            "availability_zone":       f"{REGION}a",
            "map_public_ip_on_launch": False,
            "tags":                    {},
        }
        state_resource = extract_from_state("aws_subnet", "public", state_attrs)

        cloud_resources = aws_provider.fetch("aws_subnet")
        cloud_resource = next((r for r in cloud_resources if r.id == subnet_id), None)
        assert cloud_resource is not None

        assert state_resource.attributes == cloud_resource.attributes, (
            f"Subnet attribute mismatch:\n"
            f"  state: {state_resource.attributes}\n"
            f"  cloud: {cloud_resource.attributes}"
        )

    def test_security_group_parity(self, aws_provider, ec2_client):
        """Security group with ingress rules: both extractors identical."""
        vpc_id = _create_vpc(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName="web-sg",
            Description="Web security group",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]

        # Add ingress rules
        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                },
            ],
        )

        # State-side: ingress in the order tfstate might declare them
        # (the extractor sorts them, so order here doesn't matter)
        state_attrs = {
            "id":          sg_id,
            "name":        "web-sg",
            "description": "Web security group",
            "vpc_id":      vpc_id,
            "ingress": [
                {
                    "protocol": "tcp", "from_port": 80, "to_port": 80,
                    "cidr_blocks": ["0.0.0.0/0"],
                    "ipv6_cidr_blocks": [], "security_groups": [],
                },
                {
                    "protocol": "tcp", "from_port": 443, "to_port": 443,
                    "cidr_blocks": ["0.0.0.0/0"],
                    "ipv6_cidr_blocks": [], "security_groups": [],
                },
            ],
            "egress": [],
            "tags": {},
        }
        state_resource = extract_from_state("aws_security_group", "web_sg", state_attrs)

        cloud_resources = aws_provider.fetch("aws_security_group")
        cloud_resource = next((r for r in cloud_resources if r.id == sg_id), None)
        assert cloud_resource is not None

        # Compare ingress rules
        assert (
            state_resource.attributes["ingress_rules"] ==
            cloud_resource.attributes["ingress_rules"]
        ), (
            f"SG ingress rule mismatch:\n"
            f"  state: {state_resource.attributes['ingress_rules']}\n"
            f"  cloud: {cloud_resource.attributes['ingress_rules']}"
        )

    def test_s3_bucket_parity(self, aws_provider, s3_client):
        """S3 bucket with versioning and encryption: both extractors identical."""
        bucket = "parity-test-bucket"
        s3_client.create_bucket(Bucket=bucket)
        s3_client.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        s3_client.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256",
                    },
                }],
            },
        )

        # State-side
        state_attrs = {
            "id":     bucket,
            "bucket": bucket,
            "versioning": [{"enabled": True, "mfa_delete": False}],
            "server_side_encryption_configuration": [{
                "rule": [{
                    "apply_server_side_encryption_by_default": [{
                        "sse_algorithm": "AES256",
                        "kms_master_key_id": "",
                    }],
                }],
            }],
            "logging": [],
            "tags": {},
        }
        state_resource = extract_from_state("aws_s3_bucket", "app_data", state_attrs)

        cloud_resources = aws_provider.fetch("aws_s3_bucket")
        cloud_resource = next((r for r in cloud_resources if r.id == bucket), None)
        assert cloud_resource is not None

        # Check the key attributes match
        assert state_resource.attributes["versioning_enabled"] == \
               cloud_resource.attributes["versioning_enabled"]
        assert state_resource.attributes["sse_algorithm"] == \
               cloud_resource.attributes["sse_algorithm"]
        assert state_resource.attributes["block_public_acls"] == \
               cloud_resource.attributes["block_public_acls"]
        assert state_resource.attributes["logging_target_bucket"] == \
               cloud_resource.attributes["logging_target_bucket"]


# ---------------------------------------------------------------------------
# Unsupported type
# ---------------------------------------------------------------------------

@mock_aws
class TestUnsupportedType:

    def test_unsupported_type_returns_empty_list(self, aws_provider):
        result = aws_provider.fetch("aws_lambda_function")
        assert result == []
