"""
driftctl/models.py
All shared dataclasses. No I/O, no AWS, no dependencies beyond stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# SGRule — a single security group inbound or outbound rule
# Frozen so it is hashable and can be sorted.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class SGRule:
    protocol: str                       # "tcp", "udp", "icmp", "-1" (all traffic)
    from_port: int
    to_port: int
    cidr_blocks: tuple[str, ...]        # sorted IPv4 CIDRs
    ipv6_cidr_blocks: tuple[str, ...]   # sorted IPv6 CIDRs
    source_sg_ids: tuple[str, ...]      # sorted source security group IDs


# ---------------------------------------------------------------------------
# Resource — the normalised representation of one infrastructure resource.
# Both the State Extractor and the Cloud Extractor produce this.
# The Drift Engine consumes it from both sides.
# ---------------------------------------------------------------------------

@dataclass
class Resource:
    type: str
    """
    Terraform resource type.
    One of: aws_instance, aws_vpc, aws_subnet,
            aws_security_group, aws_s3_bucket
    """

    id: str
    """
    Match key — used to pair expected with actual.
    EC2 / VPC / SG / Subnet : the AWS resource ID (i-…, vpc-…, sg-…, subnet-…)
    S3                       : the bucket name
    """

    name: str | None
    """
    Terraform logical name (e.g. "main", "web_server").
    Present only on the expected (state) side; None on the actual side.
    """

    attributes: dict
    """
    Normalised, comparable attributes. Both extractors MUST use identical
    keys and value types for the same resource type or false drift is reported.
    See SPEC Section 7.4 for the per-type contract.
    """

    tags: dict[str, str]
    """
    Tags extracted separately so tag drift is reported independently
    from attribute drift. Format: {"env": "prod", "Owner": "platform"}
    """

    source: str
    """
    "expected" = came from tfstate
    "actual"   = came from live AWS API
    """


# ---------------------------------------------------------------------------
# DriftResult — output for one resource after drift detection +
# remediation generation.
# ---------------------------------------------------------------------------

@dataclass
class DriftResult:
    type: str
    id: str
    name: str | None   # terraform logical name (from expected side if present)

    status: str
    """
    MISSING   — in state, gone from cloud (deleted out-of-band)
    UNMANAGED — in cloud, not in state (created out-of-band)
    MODIFIED  — in both, one or more attributes differ
    TAG_DRIFT — in both, attributes identical, tags differ
    IN_SYNC   — identical on both sides (shown only with --verbose)
    """

    attribute_diffs: dict = field(default_factory=dict)
    """
    {field_name: {"expected": <value>, "actual": <value>}}
    Empty for MISSING, UNMANAGED, TAG_DRIFT, IN_SYNC.
    """

    tag_diffs: dict = field(default_factory=dict)
    """
    {tag_key: {"expected": <value or None>, "actual": <value or None>}}
    None means the tag was absent on that side.
    """

    remediation: str | None = None
    """
    ★ YOUR ADDITION
    Advisory Terraform command to reconcile this drift.
    None only for IN_SYNC. Populated by engine/remediate.py.
    """


# ---------------------------------------------------------------------------
# ScanReport — container for one complete scan run.
# ---------------------------------------------------------------------------

@dataclass
class ScanReport:
    scan_id: str           # UUID
    created_at: str        # ISO-8601 e.g. "2025-06-01T09:00:00Z"
    state_path: str        # state source — local path or "s3://bucket/key"
    region: str
    workspace: str | None  # workspace name if run from workspace config
    results: list[DriftResult] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Computed properties (derived from results, not stored separately)
    # ------------------------------------------------------------------

    @property
    def total_resources(self) -> int:
        return len(self.results)

    @property
    def drifted(self) -> list[DriftResult]:
        return [r for r in self.results if r.status != "IN_SYNC"]

    @property
    def drifted_count(self) -> int:
        return len(self.drifted)

    @property
    def missing_count(self) -> int:
        return sum(1 for r in self.results if r.status == "MISSING")

    @property
    def unmanaged_count(self) -> int:
        return sum(1 for r in self.results if r.status == "UNMANAGED")

    @property
    def modified_count(self) -> int:
        return sum(1 for r in self.results if r.status == "MODIFIED")

    @property
    def tag_drift_count(self) -> int:
        return sum(1 for r in self.results if r.status == "TAG_DRIFT")

    @property
    def exit_code(self) -> int:
        """0 = no drift, 1 = drift detected, 2 = error (set externally)."""
        return 1 if self.drifted_count > 0 else 0

    def summary(self) -> dict:
        return {
            "total_resources": self.total_resources,
            "drifted":         self.drifted_count,
            "missing":         self.missing_count,
            "unmanaged":       self.unmanaged_count,
            "modified":        self.modified_count,
            "tag_drift":       self.tag_drift_count,
        }


# ---------------------------------------------------------------------------
# Workspace — a named, saved scan configuration.
# ---------------------------------------------------------------------------

@dataclass
class Workspace:
    id: str
    name: str                  # "prod", "staging", "local-dev"
    provider: str              # "aws" (only value for now)
    state_backend: str         # "local" | "s3"
    state_path: str            # local: file path  |  s3: "s3://bucket/key"
    state_region: str | None   # only relevant for s3 backend
    region: str                # AWS region to scan
    detect_unmanaged: bool
    schedule_cron: str | None  # cron expression; None = no schedule
    created_at: str            # ISO-8601
    last_scan_id: str | None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StateReadError(Exception):
    """Raised when the state file cannot be read or parsed."""


class UnsupportedStateVersionError(Exception):
    """Raised when the tfstate version is not 4."""


class UnsupportedProviderError(Exception):
    """Raised when an unknown cloud provider is requested."""
