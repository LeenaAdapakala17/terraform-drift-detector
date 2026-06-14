"""
tests/test_state_extractor.py

Unit tests for driftctl/state/extractor.py

Tests per-type normalisation: given a raw tfstate attributes block,
assert that the output Resource has the correct field values and tags.
Also tests edge cases: unknown types, missing fields, type coercions.
"""

from __future__ import annotations

from driftctl.models import Resource, SGRule
from driftctl.state.extractor import extract_from_state


# ---------------------------------------------------------------------------
# aws_instance
# ---------------------------------------------------------------------------

class TestExtractInstance:

    BASE_ATTRS = {
        "id": "i-0abc1234567890def",
        "ami": "ami-0c55b159cbfafe1f0",
        "instance_type": "t2.micro",
        "subnet_id": "subnet-0a1b2c3d4e5f67890",
        "key_name": "my-keypair",
        "associate_public_ip_address": False,
        "monitoring": False,
        "iam_instance_profile": "",
        "vpc_security_group_ids": ["sg-bbb", "sg-aaa"],  # unsorted on purpose
        "ebs_optimized": False,
        "root_block_device": [{"volume_size": 8, "volume_type": "gp2"}],
        "tags": {"env": "production", "Name": "web-server"},
    }

    def test_returns_resource(self):
        r = extract_from_state("aws_instance", "web_server", self.BASE_ATTRS)
        assert isinstance(r, Resource)

    def test_type_and_id(self):
        r = extract_from_state("aws_instance", "web_server", self.BASE_ATTRS)
        assert r.type == "aws_instance"
        assert r.id == "i-0abc1234567890def"
        assert r.name == "web_server"
        assert r.source == "expected"

    def test_instance_type(self):
        r = extract_from_state("aws_instance", "web_server", self.BASE_ATTRS)
        assert r.attributes["instance_type"] == "t2.micro"

    def test_sg_ids_are_sorted(self):
        """vpc_security_group_ids must be sorted regardless of input order."""
        r = extract_from_state("aws_instance", "web_server", self.BASE_ATTRS)
        assert r.attributes["vpc_security_group_ids"] == ["sg-aaa", "sg-bbb"]

    def test_bool_coercion_from_string(self):
        """'true' / 'false' strings should become Python bools."""
        attrs = {**self.BASE_ATTRS, "monitoring": "true", "ebs_optimized": "false"}
        r = extract_from_state("aws_instance", "web_server", attrs)
        assert r.attributes["monitoring"] is True
        assert r.attributes["ebs_optimized"] is False

    def test_root_block_device(self):
        r = extract_from_state("aws_instance", "web_server", self.BASE_ATTRS)
        assert r.attributes["root_block_device_size"] == 8
        assert r.attributes["root_block_device_type"] == "gp2"

    def test_tags_extracted(self):
        r = extract_from_state("aws_instance", "web_server", self.BASE_ATTRS)
        assert r.tags == {"env": "production", "Name": "web-server"}

    def test_empty_tags(self):
        attrs = {**self.BASE_ATTRS, "tags": {}}
        r = extract_from_state("aws_instance", "web_server", attrs)
        assert r.tags == {}

    def test_missing_tags_key(self):
        attrs = {k: v for k, v in self.BASE_ATTRS.items() if k != "tags"}
        r = extract_from_state("aws_instance", "web_server", attrs)
        assert r.tags == {}


# ---------------------------------------------------------------------------
# aws_vpc
# ---------------------------------------------------------------------------

class TestExtractVpc:

    BASE_ATTRS = {
        "id": "vpc-0a1b2c3d4e5f67890",
        "cidr_block": "10.0.0.0/16",
        "instance_tenancy": "default",
        "enable_dns_support": True,
        "enable_dns_hostnames": True,
        "tags": {"Name": "main-vpc", "env": "production"},
    }

    def test_returns_resource(self):
        r = extract_from_state("aws_vpc", "main", self.BASE_ATTRS)
        assert isinstance(r, Resource)

    def test_fields(self):
        r = extract_from_state("aws_vpc", "main", self.BASE_ATTRS)
        assert r.attributes["cidr_block"] == "10.0.0.0/16"
        assert r.attributes["instance_tenancy"] == "default"
        assert r.attributes["enable_dns_support"] is True
        assert r.attributes["enable_dns_hostnames"] is True

    def test_bool_fields_from_string(self):
        attrs = {**self.BASE_ATTRS, "enable_dns_support": "false", "enable_dns_hostnames": "true"}
        r = extract_from_state("aws_vpc", "main", attrs)
        assert r.attributes["enable_dns_support"] is False
        assert r.attributes["enable_dns_hostnames"] is True


# ---------------------------------------------------------------------------
# aws_subnet
# ---------------------------------------------------------------------------

class TestExtractSubnet:

    BASE_ATTRS = {
        "id": "subnet-0a1b2c3d4e5f67890",
        "vpc_id": "vpc-0a1b2c3d4e5f67890",
        "cidr_block": "10.0.1.0/24",
        "availability_zone": "us-east-1a",
        "map_public_ip_on_launch": False,
        "tags": {"Name": "public-subnet"},
    }

    def test_fields(self):
        r = extract_from_state("aws_subnet", "public", self.BASE_ATTRS)
        assert r.attributes["vpc_id"] == "vpc-0a1b2c3d4e5f67890"
        assert r.attributes["cidr_block"] == "10.0.1.0/24"
        assert r.attributes["availability_zone"] == "us-east-1a"
        assert r.attributes["map_public_ip_on_launch"] is False

    def test_bool_coercion(self):
        attrs = {**self.BASE_ATTRS, "map_public_ip_on_launch": "true"}
        r = extract_from_state("aws_subnet", "public", attrs)
        assert r.attributes["map_public_ip_on_launch"] is True


# ---------------------------------------------------------------------------
# aws_security_group
# ---------------------------------------------------------------------------

class TestExtractSecurityGroup:

    BASE_ATTRS = {
        "id": "sg-0111aaa222bbb333c",
        "name": "web-sg",
        "description": "Security group for web servers",
        "vpc_id": "vpc-0a1b2c3d4e5f67890",
        "ingress": [
            {
                "protocol": "tcp", "from_port": 443, "to_port": 443,
                "cidr_blocks": ["0.0.0.0/0"], "ipv6_cidr_blocks": [],
                "security_groups": [],
            },
            {
                "protocol": "tcp", "from_port": 80, "to_port": 80,
                "cidr_blocks": ["0.0.0.0/0"], "ipv6_cidr_blocks": [],
                "security_groups": [],
            },
        ],
        "egress": [
            {
                "protocol": "-1", "from_port": 0, "to_port": 0,
                "cidr_blocks": ["0.0.0.0/0"], "ipv6_cidr_blocks": [],
                "security_groups": [],
            },
        ],
        "tags": {"Name": "web-sg", "env": "production"},
    }

    def test_basic_fields(self):
        r = extract_from_state("aws_security_group", "web_sg", self.BASE_ATTRS)
        assert r.attributes["name"] == "web-sg"
        assert r.attributes["description"] == "Security group for web servers"
        assert r.attributes["vpc_id"] == "vpc-0a1b2c3d4e5f67890"

    def test_ingress_rules_are_sorted(self):
        """Ingress rules should be sorted by (protocol, from_port, to_port)."""
        r = extract_from_state("aws_security_group", "web_sg", self.BASE_ATTRS)
        ingress = r.attributes["ingress_rules"]
        assert len(ingress) == 2
        # port 80 sorts before port 443
        assert ingress[0].from_port == 80
        assert ingress[1].from_port == 443

    def test_ingress_rule_is_sgrule(self):
        r = extract_from_state("aws_security_group", "web_sg", self.BASE_ATTRS)
        rule = r.attributes["ingress_rules"][0]
        assert isinstance(rule, SGRule)
        assert rule.protocol == "tcp"
        assert rule.cidr_blocks == ("0.0.0.0/0",)

    def test_egress_rules(self):
        r = extract_from_state("aws_security_group", "web_sg", self.BASE_ATTRS)
        egress = r.attributes["egress_rules"]
        assert len(egress) == 1
        assert egress[0].protocol == "-1"

    def test_cidr_blocks_are_sorted(self):
        """CIDR blocks within a rule should be sorted."""
        attrs = {
            **self.BASE_ATTRS,
            "ingress": [{
                "protocol": "tcp", "from_port": 22, "to_port": 22,
                "cidr_blocks": ["10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12"],
                "ipv6_cidr_blocks": [], "security_groups": [],
            }],
        }
        r = extract_from_state("aws_security_group", "web_sg", attrs)
        rule = r.attributes["ingress_rules"][0]
        assert rule.cidr_blocks == tuple(sorted(["10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12"]))

    def test_empty_ingress(self):
        attrs = {**self.BASE_ATTRS, "ingress": []}
        r = extract_from_state("aws_security_group", "web_sg", attrs)
        assert r.attributes["ingress_rules"] == []


# ---------------------------------------------------------------------------
# aws_s3_bucket
# ---------------------------------------------------------------------------

class TestExtractS3Bucket:

    BASE_ATTRS = {
        "id": "my-app-data-bucket",
        "bucket": "my-app-data-bucket",
        "versioning": [{"enabled": True, "mfa_delete": False}],
        "server_side_encryption_configuration": [
            {
                "rule": [
                    {
                        "apply_server_side_encryption_by_default": [
                            {"sse_algorithm": "AES256", "kms_master_key_id": ""}
                        ]
                    }
                ]
            }
        ],
        "logging": [],
        "tags": {"Name": "app-data", "env": "production", "CostCenter": "engineering"},
    }

    def test_versioning_enabled(self):
        r = extract_from_state("aws_s3_bucket", "app_data", self.BASE_ATTRS)
        assert r.attributes["versioning_enabled"] is True

    def test_versioning_disabled(self):
        attrs = {**self.BASE_ATTRS, "versioning": [{"enabled": False}]}
        r = extract_from_state("aws_s3_bucket", "app_data", attrs)
        assert r.attributes["versioning_enabled"] is False

    def test_versioning_missing(self):
        attrs = {**self.BASE_ATTRS, "versioning": []}
        r = extract_from_state("aws_s3_bucket", "app_data", attrs)
        assert r.attributes["versioning_enabled"] is False

    def test_sse_algorithm(self):
        r = extract_from_state("aws_s3_bucket", "app_data", self.BASE_ATTRS)
        assert r.attributes["sse_algorithm"] == "AES256"

    def test_sse_algorithm_none_when_missing(self):
        attrs = {**self.BASE_ATTRS, "server_side_encryption_configuration": []}
        r = extract_from_state("aws_s3_bucket", "app_data", attrs)
        assert r.attributes["sse_algorithm"] is None

    def test_tags_extracted(self):
        r = extract_from_state("aws_s3_bucket", "app_data", self.BASE_ATTRS)
        assert r.tags["CostCenter"] == "engineering"
        assert r.tags["env"] == "production"

    def test_no_logging(self):
        r = extract_from_state("aws_s3_bucket", "app_data", self.BASE_ATTRS)
        assert r.attributes["logging_target_bucket"] is None
        assert r.attributes["logging_target_prefix"] is None

    def test_public_access_block_defaults_false(self):
        """When pab is not present, all four flags should default to False."""
        r = extract_from_state("aws_s3_bucket", "app_data", self.BASE_ATTRS)
        assert r.attributes["block_public_acls"] is False
        assert r.attributes["ignore_public_acls"] is False
        assert r.attributes["block_public_policy"] is False
        assert r.attributes["restrict_public_buckets"] is False


# ---------------------------------------------------------------------------
# Unknown type
# ---------------------------------------------------------------------------

class TestUnknownType:

    def test_returns_none_for_unknown_type(self):
        """Unsupported resource types should return None without raising."""
        result = extract_from_state(
            "aws_lambda_function", "my_func",
            {"id": "my-func", "function_name": "my-func"},
        )
        assert result is None

    def test_returns_none_for_empty_type(self):
        result = extract_from_state("", "something", {"id": "x"})
        assert result is None
