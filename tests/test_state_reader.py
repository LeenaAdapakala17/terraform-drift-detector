"""
tests/test_state_reader.py

Unit tests for driftctl/state/reader.py

Tests:
  - Parse the sample tfstate from local disk
  - Skip data source resources (mode != "managed")
  - Raise UnsupportedStateVersionError for wrong version
  - Raise StateReadError for missing file
  - Raise StateReadError for invalid JSON
  - S3 backend: download and parse (moto mock)
  - S3 backend: raise StateReadError on missing key
  - S3 backend: raise StateReadError on invalid S3 URI
"""

from __future__ import annotations

import json
import os

import boto3
import pytest
from moto import mock_aws

from driftctl.models import StateReadError, UnsupportedStateVersionError
from driftctl.state.reader import read_state

# Path to the hand-crafted sample state file
SAMPLE_TFSTATE = os.path.join(
    os.path.dirname(__file__), "..", "testdata", "sample.tfstate"
)


# ---------------------------------------------------------------------------
# Local backend tests
# ---------------------------------------------------------------------------

class TestLocalStateReader:

    def test_reads_sample_tfstate(self):
        """Should parse the sample file and return 5 managed resources."""
        records = read_state(SAMPLE_TFSTATE)
        assert len(records) == 5, (
            f"Expected 5 managed resources, got {len(records)}: "
            f"{[r['type'] + '.' + r['name'] for r in records]}"
        )

    def test_returns_correct_types(self):
        """Should return all five resource types."""
        records = read_state(SAMPLE_TFSTATE)
        types = {r["type"] for r in records}
        assert types == {
            "aws_instance",
            "aws_vpc",
            "aws_subnet",
            "aws_security_group",
            "aws_s3_bucket",
        }

    def test_skips_data_sources(self):
        """Mode 'data' resources must be skipped."""
        records = read_state(SAMPLE_TFSTATE)
        types = [r["type"] for r in records]
        assert "aws_availability_zones" not in types

    def test_record_has_required_keys(self):
        """Each record must have type, name, and attributes keys."""
        records = read_state(SAMPLE_TFSTATE)
        for record in records:
            assert "type" in record
            assert "name" in record
            assert "attributes" in record
            assert isinstance(record["attributes"], dict)

    def test_instance_record_name(self):
        """The EC2 instance record should have name 'web_server'."""
        records = read_state(SAMPLE_TFSTATE)
        instance_records = [r for r in records if r["type"] == "aws_instance"]
        assert len(instance_records) == 1
        assert instance_records[0]["name"] == "web_server"

    def test_raises_for_missing_file(self):
        """Should raise StateReadError when the file does not exist."""
        with pytest.raises(StateReadError, match="not found"):
            read_state("/nonexistent/path/terraform.tfstate")

    def test_raises_for_invalid_json(self, tmp_path):
        """Should raise StateReadError when the file contains invalid JSON."""
        bad_file = tmp_path / "bad.tfstate"
        bad_file.write_text("this is not json {{{")
        with pytest.raises(StateReadError, match="Invalid JSON"):
            read_state(str(bad_file))

    def test_raises_for_unsupported_version(self, tmp_path):
        """Should raise UnsupportedStateVersionError for version != 4."""
        state_v3 = {"version": 3, "resources": []}
        state_file = tmp_path / "v3.tfstate"
        state_file.write_text(json.dumps(state_v3))
        with pytest.raises(UnsupportedStateVersionError, match="version 3"):
            read_state(str(state_file))

    def test_empty_resources_returns_empty_list(self, tmp_path):
        """Should return an empty list when resources array is empty."""
        state = {"version": 4, "resources": []}
        state_file = tmp_path / "empty.tfstate"
        state_file.write_text(json.dumps(state))
        records = read_state(str(state_file))
        assert records == []

    def test_skips_resource_with_no_instances(self, tmp_path):
        """Resources with empty instances list should be skipped silently."""
        state = {
            "version": 4,
            "resources": [
                {
                    "mode": "managed",
                    "type": "aws_instance",
                    "name": "orphan",
                    "instances": [],
                }
            ],
        }
        state_file = tmp_path / "no_instances.tfstate"
        state_file.write_text(json.dumps(state))
        records = read_state(str(state_file))
        assert records == []


# ---------------------------------------------------------------------------
# S3 backend tests (moto)
# ---------------------------------------------------------------------------

@mock_aws
class TestS3StateReader:

    BUCKET = "my-tf-state-bucket"
    KEY = "prod/terraform.tfstate"
    REGION = "us-east-1"

    def _upload_state(self, content: dict) -> None:
        """Helper: create a bucket and upload a state file."""
        s3 = boto3.client("s3", region_name=self.REGION)
        s3.create_bucket(Bucket=self.BUCKET)
        s3.put_object(
            Bucket=self.BUCKET,
            Key=self.KEY,
            Body=json.dumps(content).encode(),
        )

    def test_reads_state_from_s3(self):
        """Should download and parse a tfstate file from S3."""
        with open(SAMPLE_TFSTATE) as f:
            state_content = json.load(f)
        self._upload_state(state_content)

        uri = f"s3://{self.BUCKET}/{self.KEY}"
        records = read_state(uri, region=self.REGION)
        assert len(records) == 5

    def test_s3_skips_data_sources(self):
        """Data source resources should be skipped from S3 state too."""
        with open(SAMPLE_TFSTATE) as f:
            state_content = json.load(f)
        self._upload_state(state_content)

        uri = f"s3://{self.BUCKET}/{self.KEY}"
        records = read_state(uri, region=self.REGION)
        types = [r["type"] for r in records]
        assert "aws_availability_zones" not in types

    def test_raises_for_missing_s3_key(self):
        """Should raise StateReadError when the key does not exist in S3."""
        s3 = boto3.client("s3", region_name=self.REGION)
        s3.create_bucket(Bucket=self.BUCKET)

        uri = f"s3://{self.BUCKET}/nonexistent/key.tfstate"
        with pytest.raises(StateReadError, match="not found in S3"):
            read_state(uri, region=self.REGION)

    def test_raises_for_invalid_s3_uri(self):
        """Should raise StateReadError for a malformed S3 URI."""
        with pytest.raises(StateReadError, match="Invalid S3 URI"):
            read_state("s3://onlybucket", region=self.REGION)

    def test_raises_for_invalid_json_in_s3(self):
        """Should raise StateReadError when S3 object contains invalid JSON."""
        s3 = boto3.client("s3", region_name=self.REGION)
        s3.create_bucket(Bucket=self.BUCKET)
        s3.put_object(Bucket=self.BUCKET, Key=self.KEY, Body=b"not json {{")

        uri = f"s3://{self.BUCKET}/{self.KEY}"
        with pytest.raises(StateReadError, match="Invalid JSON"):
            read_state(uri, region=self.REGION)

    def test_s3_unsupported_version_raises(self):
        """Should raise UnsupportedStateVersionError from S3 state."""
        self._upload_state({"version": 3, "resources": []})
        uri = f"s3://{self.BUCKET}/{self.KEY}"
        with pytest.raises(UnsupportedStateVersionError):
            read_state(uri, region=self.REGION)
