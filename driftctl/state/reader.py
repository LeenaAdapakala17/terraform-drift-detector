"""
driftctl/state/reader.py

Reads a Terraform state file from either:
  - a local filesystem path   e.g. "./terraform.tfstate"
  - an S3 URI                 e.g. "s3://my-bucket/prod/terraform.tfstate"

Returns the raw JSON dict of the tfstate. Does NOT normalise — that is
the extractor's job.

Raises:
  StateReadError               — file not found, S3 error, JSON parse failure
  UnsupportedStateVersionError — tfstate version is not 4
"""

from __future__ import annotations

import json
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from driftctl.models import StateReadError, UnsupportedStateVersionError

logger = logging.getLogger(__name__)

SUPPORTED_STATE_VERSION = 4


def read_state(source: str, region: str | None = None) -> list[dict]:
    """
    Read a Terraform state file and return a list of raw resource records.

    Each record is a dict:
        {
            "type":       str,   # e.g. "aws_instance"
            "name":       str,   # terraform logical name e.g. "web_server"
            "attributes": dict,  # raw tfstate instance attributes
        }

    Args:
        source: Local file path OR "s3://bucket/key"
        region: AWS region (required for S3 backend; ignored for local)

    Returns:
        List of raw resource record dicts (managed resources only).

    Raises:
        StateReadError
        UnsupportedStateVersionError
    """
    raw = _load_raw(source, region)
    _validate_version(raw, source)
    return _extract_records(raw)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_raw(source: str, region: str | None) -> dict:
    """Load and JSON-parse the state file from local disk or S3."""
    if source.startswith("s3://"):
        return _load_from_s3(source, region)
    return _load_from_local(source)


def _load_from_local(path: str) -> dict:
    """Read a local .tfstate file."""
    logger.debug("Reading local state file: %s", path)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise StateReadError(f"State file not found: {path}")
    except json.JSONDecodeError as exc:
        raise StateReadError(f"Invalid JSON in state file {path}: {exc}") from exc
    except OSError as exc:
        raise StateReadError(f"Cannot read state file {path}: {exc}") from exc


def _load_from_s3(uri: str, region: str | None) -> dict:
    """
    Download a .tfstate file from S3 and parse it.

    URI format: s3://bucket-name/path/to/terraform.tfstate
    """
    logger.debug("Reading S3 state file: %s", uri)

    # Parse s3://bucket/key
    without_scheme = uri[len("s3://"):]
    parts = without_scheme.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise StateReadError(
            f"Invalid S3 URI '{uri}'. Expected format: s3://bucket-name/path/to/file.tfstate"
        )
    bucket, key = parts[0], parts[1]

    try:
        session = boto3.Session(region_name=region)
        s3 = session.client("s3")
        response = s3.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read()
        return json.loads(content)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "NoSuchKey":
            raise StateReadError(
                f"State file not found in S3: s3://{bucket}/{key}"
            ) from exc
        if error_code in ("NoSuchBucket", "AccessDenied", "AllAccessDisabled"):
            raise StateReadError(
                f"S3 access error ({error_code}) reading s3://{bucket}/{key}: {exc}"
            ) from exc
        raise StateReadError(
            f"S3 error reading s3://{bucket}/{key}: {exc}"
        ) from exc
    except BotoCoreError as exc:
        raise StateReadError(
            f"AWS connection error reading {uri}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise StateReadError(
            f"Invalid JSON in S3 state file s3://{bucket}/{key}: {exc}"
        ) from exc


def _validate_version(data: dict, source: str) -> None:
    """Assert the tfstate format version is 4 (Terraform 0.12+)."""
    version = data.get("version")
    if version != SUPPORTED_STATE_VERSION:
        raise UnsupportedStateVersionError(
            f"Unsupported Terraform state version {version!r} in {source}. "
            f"driftctl supports version {SUPPORTED_STATE_VERSION} (Terraform 0.12+)."
        )


def _extract_records(data: dict) -> list[dict]:
    """
    Iterate the resources[] array and yield managed resource records.

    Terraform state v4 structure:
    {
      "version": 4,
      "resources": [
        {
          "mode": "managed",      ← only these; skip "data" sources
          "type": "aws_instance",
          "name": "web_server",
          "instances": [
            {
              "attributes": { ... }  ← the normalised attribute block
            }
          ]
        }
      ]
    }

    Returns one record per instance (most resources have one instance,
    but count resources can have multiple).
    """
    records = []
    resources = data.get("resources", [])

    for resource in resources:
        mode = resource.get("mode", "")
        if mode != "managed":
            # Skip data sources, module references, etc.
            logger.debug(
                "Skipping non-managed resource: %s.%s (mode=%s)",
                resource.get("type"), resource.get("name"), mode,
            )
            continue

        resource_type = resource.get("type", "")
        resource_name = resource.get("name", "")
        instances = resource.get("instances", [])

        if not instances:
            logger.debug("Resource %s.%s has no instances, skipping", resource_type, resource_name)
            continue

        for instance in instances:
            attributes = instance.get("attributes", {})
            if not attributes:
                continue
            records.append({
                "type":       resource_type,
                "name":       resource_name,
                "attributes": attributes,
            })

    logger.info("State reader: found %d managed resource instances", len(records))
    return records
