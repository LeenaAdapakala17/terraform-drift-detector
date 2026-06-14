# driftctl — Complete Technical Specification
**Version:** v2.0 (Final)
**Language:** Python 3.11+
**Foundation:** Based on Abhishek Veeramalla's terraform-drift-detector
**Extensions:** Remediation Hints + Enhanced Web Dashboard
**AWS Account:** Free-tier safe (read-only API calls only)

---

## Table of Contents

1.  [Project Overview](#1-project-overview)
2.  [How Drift Detection Works](#2-how-drift-detection-works)
3.  [What This Project Detects](#3-what-this-project-detects)
4.  [Complete Feature Set](#4-complete-feature-set)
5.  [System Architecture](#5-system-architecture)
6.  [Supported AWS Resources](#6-supported-aws-resources)
7.  [Data Models](#7-data-models)
8.  [Component Specifications](#8-component-specifications)
9.  [Drift Engine](#9-drift-engine)
10. [Remediation Hints ★](#10-remediation-hints-)
11. [CLI](#11-cli)
12. [REST API](#12-rest-api)
13. [Web Dashboard ★](#13-web-dashboard-)
14. [Cron Scheduler](#14-cron-scheduler)
15. [SQLite Persistence](#15-sqlite-persistence)
16. [Configuration](#16-configuration)
17. [Project Layout](#17-project-layout)
18. [Testing Strategy](#18-testing-strategy)
19. [Packaging & Developer Experience](#19-packaging--developer-experience)
20. [Build Phases](#20-build-phases)
21. [Dependencies](#21-dependencies)

★ = built on top of Abhishek's foundation

---

## 1. Project Overview

driftctl compares Terraform state files against live AWS infrastructure to detect
configuration drift — without running `terraform plan` or `terraform apply`.

This project is a Python port of Abhishek Veeramalla's terraform-drift-detector
(originally written in Go), with two additions built on top:

1. **Remediation Hints** — for every drift result, the exact Terraform command
   to fix it.
2. **Enhanced Web Dashboard** — scan history, per-resource drill-down with field
   diffs, and a drift trends chart.

Everything else — state reader, cloud fetcher, drift engine, CLI, REST API,
scheduler, SQLite persistence, YAML config — mirrors Abhishek's original feature
set, ported to Python.

### Why Python over Go
- More accessible for the DevOps/Platform engineering audience
- boto3 is the natural AWS SDK for Python
- FastAPI, Typer, Rich, APScheduler are mature, well-documented Python libraries
- moto enables full offline AWS mocking for tests

### Free-tier safety
Every AWS call in this project is a `describe_*` / `get_*` / `list_*` read.
AWS does not bill for these metadata API calls. The tool never creates, modifies,
or deletes any resource.

---

## 2. How Drift Detection Works

### The problem with terraform plan
The standard way to catch drift is `terraform plan`. But it has three problems:

**Problem 1 — It requires running Terraform.**
You need the right credentials, the right backend configured, the right workspace
active. In most teams this means drift goes undetected between planned runs.

**Problem 2 — It compares HCL to state, not state to cloud.**
`terraform plan` reads your `.tf` files and your state file and shows what would
change if you applied. It does not independently verify that what is in the state
file matches what is actually running in AWS. If someone manually changed a
resource in the console, plan won't catch it until the next apply — by which
point you may have already overwritten their change.

**Problem 3 — Nobody runs it consistently.**
Plan is a manual step. Drift accumulates silently in the gaps.

### What driftctl does differently
Instead of running Terraform, driftctl directly compares two things:

```
What Terraform thinks exists  ←─ .tfstate file (expected model)
What AWS says actually exists ←─ live API calls (actual model)
```

These two models are normalised into the same shape, then diffed field by field.
No Terraform binary needed. No apply risk. No side effects. Just a read.

### The normalisation challenge
This is where the real engineering work is. The same resource looks different
depending on where you read it from:

- tfstate uses `instance_type` (snake_case). boto3 returns `InstanceType` (PascalCase).
- Security group rules in tfstate are ordered by declaration. boto3 returns them
  in AWS's internal order. Same rules, different order — looks like drift without
  normalisation.
- S3 bucket configuration in tfstate is one block. In AWS it lives across five
  separate API calls (versioning, encryption, tagging, public access block,
  logging). The fetcher must assemble all five into one normalised resource.

Both extractors (state-side and cloud-side) must produce identical output for
the same resource, or everything looks like drift.

---

## 3. What This Project Detects

### MISSING — deleted in the cloud, still in Terraform state
The resource exists in `.tfstate` but is gone from AWS. It was deleted
out-of-band (console, CLI, another tool). Terraform still thinks it exists.
The next `terraform apply` will attempt to recreate it.

**Example:** An engineer deleted an EC2 instance in the console to save cost.
Terraform state still lists it. The next apply recreates it with an unexpected
bill.

### UNMANAGED — in the cloud, not in Terraform state
The resource exists live in AWS but has no entry in Terraform state. It was
created out-of-band and has no Terraform lifecycle — no plan, no destroy, no
state lock.

**Example:** A developer created a security group in the console to unblock a
test and never removed or imported it. It sits outside Terraform forever.

### MODIFIED — attributes changed out-of-band
The resource exists in both state and cloud but one or more attributes differ.
The live configuration no longer matches the Terraform-declared intent.

**Example:** The `instance_type` in state is `t2.micro` but the live instance
was manually resized to `t3.small`. Or a VPC CIDR was widened. Or a security
group rule was added to open a new port.

### TAG_DRIFT — only tags changed
Tags are tracked separately because they change more frequently than attributes,
have their own operational significance (cost allocation, environment labelling,
ownership tracking), and teams often want different alerting policies for tag
drift versus structural drift.

**Example:** The `env` tag was changed from `production` to `prod`. The
`CostCenter` tag was removed. The `Owner` tag was never applied.

---

## 4. Complete Feature Set

### From Abhishek's original (ported to Python)
- Terraform state reading from **local file** and **S3 bucket**
- Live AWS resource fetching for EC2, VPC, Subnet, Security Group, S3
- Normalised resource model with field-level comparison
- Four drift classifications: MISSING, UNMANAGED, MODIFIED, TAG_DRIFT
- CLI with scan, report, workspace, schedule commands
- Output formats: JSON and table (terminal)
- `--skip-cloud` mode for offline/CI state validation
- REST API with 7 endpoints
- Basic web dashboard
- SQLite persistence (scans, results, workspaces, schedules)
- YAML configuration with workspace definitions
- Cron-based scheduled scanning via APScheduler
- Optional API key authentication (`X-API-Key` header)
- Exit codes: 0 = no drift, 1 = drift detected, 2 = error

### Added on top (your contributions)
- **Remediation hints** — exact Terraform command per drift result, shown in
  CLI, JSON, REST API, and dashboard
- **Enhanced dashboard** — scan history view, per-resource drill-down with
  field diffs and remediation, drift trends chart

---

## 5. System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                          driftctl                                    │
│                                                                      │
│  ┌─────────────────────────────────────────────┐                    │
│  │              State Reader                    │                    │
│  │  backend: local ──→ read from disk           │                    │
│  │  backend: s3    ──→ boto3 s3.get_object      │                    │
│  │  ──→ JSON parse ──→ yield managed resources  │                    │
│  └──────────────────────┬──────────────────────┘                    │
│                         │                                            │
│                         ▼                                            │
│  ┌──────────────────────────────────────────────┐                   │
│  │            State Extractor                    │                   │
│  │  per-type dispatch                            │                   │
│  │  tfstate attributes ──→ normalised Resource   │                   │
│  │  snake_case, typed fields, sorted lists       │                   │
│  └──────────────────────┬──────────────────────┘                    │
│                         │                                            │
│                         │  Expected Model: list[Resource]            │
│                         ▼                                            │
│  ┌──────────────────────────────────────────────┐                   │
│  │              Drift Engine                     │◀── Actual Model  │
│  │  index both sides on (type, id)               │         │        │
│  │  diff field by field                          │         │        │
│  │  classify each result                         │  ┌──────┴──────┐ │
│  │  MISSING / UNMANAGED / MODIFIED /             │  │Cloud Fetcher│ │
│  │  TAG_DRIFT / IN_SYNC                          │  │AWS Provider │ │
│  └──────────────────────┬──────────────────────┘   │boto3 reads  │ │
│                         │                           │EC2,VPC,SG,  │ │
│                         │                           │Subnet,S3    │ │
│                         │                           └─────────────┘ │
│                         ▼                                            │
│  ┌──────────────────────────────────────────────┐                   │
│  │        Remediation Generator  ★               │                   │
│  │  UNMANAGED  → terraform import <type> <id>    │                   │
│  │  MISSING    → terraform apply / state rm      │                   │
│  │  MODIFIED   → terraform apply + field list    │                   │
│  │  TAG_DRIFT  → terraform apply (tags)          │                   │
│  └──────────────────────┬──────────────────────┘                    │
│                         │                                            │
│                         ▼                                            │
│  ┌──────────────────────────────────────────────┐                   │
│  │              ScanReport                       │                   │
│  │  list[DriftResult]  each with:                │                   │
│  │  type, id, status, attribute_diffs,           │                   │
│  │  tag_diffs, remediation ★                     │                   │
│  └────────┬─────────────────────────────────────┘                   │
│           │                                                          │
│    ┌──────┴─────────────────────┐                                   │
│    │                            │                                   │
│    ▼                            ▼                                   │
│ ┌──────────────┐     ┌─────────────────────────┐                   │
│ │     CLI      │     │       REST API           │                   │
│ │ typer        │     │       FastAPI :8080       │                   │
│ │ table / json │     │       7 endpoints         │                   │
│ └──────────────┘     └──────────────┬───────────┘                   │
│                                     │                                │
│                              ┌──────▼──────────────────────────┐    │
│                              │   Web Dashboard  ★               │    │
│                              │   • Scan history view            │    │
│                              │   • Per-resource drill-down      │    │
│                              │   • Field diffs + remediation    │    │
│                              │   • Drift trends chart           │    │
│                              └──────────────────────────────────┘    │
│                                     │                                │
│                              ┌──────▼──────────┐                    │
│                              │     SQLite       │                    │
│                              │  driftctl.db     │                    │
│                              │  scans           │                    │
│                              │  drift_results   │                    │
│                              │  workspaces      │                    │
│                              └──────────────────┘                    │
│                                     │                                │
│                              ┌──────▼──────────┐                    │
│                              │   APScheduler    │                    │
│                              │  cron jobs per   │                    │
│                              │  workspace       │                    │
│                              └──────────────────┘                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. Supported AWS Resources

All calls are read-only. AWS does not bill for these API calls.

| Terraform type | boto3 service | API calls | Match key |
|---|---|---|---|
| `aws_instance` | `ec2` | `describe_instances` (paginated) | instance id (`i-…`) |
| `aws_vpc` | `ec2` | `describe_vpcs` (paginated) | vpc id (`vpc-…`) |
| `aws_subnet` | `ec2` | `describe_subnets` (paginated) | subnet id (`subnet-…`) |
| `aws_security_group` | `ec2` | `describe_security_groups` (paginated) | group id (`sg-…`) |
| `aws_s3_bucket` | `s3` | `list_buckets` + 5 per-bucket calls | bucket name |

### S3 per-bucket calls
The S3 fetcher calls `list_buckets` once, then for every bucket:
- `get_bucket_versioning` → `versioning_enabled`
- `get_bucket_encryption` → `sse_algorithm`
- `get_bucket_tagging` → `tags`
- `get_public_access_block` → four boolean fields
- `get_bucket_logging` → `logging_target_bucket`, `logging_target_prefix`

Each of these can raise `NoSuchBucketPolicy`, `NoSuchPublicAccessBlockConfiguration`,
`ServerSideEncryptionConfigurationNotFoundError`, etc. All are caught and treated
as "feature not enabled" (the field is set to `None` or `False`).

---

## 7. Data Models

All models in `driftctl/models.py`.

### 7.1 Resource

The normalised representation of one infrastructure resource, from either side
of the comparison.

```python
@dataclass
class Resource:
    type: str
    # Terraform resource type.
    # One of: "aws_instance", "aws_vpc", "aws_subnet",
    #         "aws_security_group", "aws_s3_bucket"

    id: str
    # The match key used to pair expected with actual.
    # EC2/VPC/SG/Subnet: the AWS resource ID ("i-…", "vpc-…", etc.)
    # S3: the bucket name

    name: str | None
    # Terraform logical name (e.g. "main", "web_server").
    # Only present on the expected (state) side. None on actual.

    attributes: dict
    # Normalised, comparable attribute dict.
    # Both extractors MUST produce identical keys and value types
    # for the same resource type. See Section 7.4 for the contract.

    tags: dict[str, str]
    # Tags extracted separately for independent tag-drift reporting.
    # Format: {"env": "prod", "Owner": "platform-team"}

    source: str
    # "expected" = came from tfstate
    # "actual"   = came from live AWS API
```

### 7.2 DriftResult

The output for one resource after the drift engine and remediation generator
have processed it.

```python
@dataclass
class DriftResult:
    type: str
    id: str
    name: str | None         # terraform logical name (from expected side)

    status: str
    # MISSING    — in state, gone from cloud
    # UNMANAGED  — in cloud, not in state
    # MODIFIED   — in both, attributes differ
    # TAG_DRIFT  — in both, only tags differ
    # IN_SYNC    — identical (included only in verbose mode)

    attribute_diffs: dict
    # {field_name: {"expected": <value>, "actual": <value>}}
    # Empty for MISSING, UNMANAGED, TAG_DRIFT, IN_SYNC

    tag_diffs: dict
    # {tag_key: {"expected": <value or None>, "actual": <value or None>}}
    # None means the tag was absent on that side

    remediation: str | None
    # ★ YOUR ADDITION
    # The exact Terraform command to fix this drift.
    # None only for IN_SYNC.
    # See Section 10 for full specification.
```

### 7.3 ScanReport

Container for one complete scan run.

```python
@dataclass
class ScanReport:
    scan_id: str             # UUID
    created_at: str          # ISO-8601 e.g. "2025-06-01T09:00:00Z"
    state_path: str          # the state source e.g. "s3://bucket/key"
    region: str
    workspace: str | None    # workspace name if run from workspace config

    results: list[DriftResult]

    # Computed from results (not stored separately):
    # total_resources  int   — unique (type, id) pairs evaluated
    # drifted_count    int   — count where status != IN_SYNC
    # missing_count    int
    # unmanaged_count  int
    # modified_count   int
    # tag_drift_count  int
    # exit_code        int   — 0, 1, or 2
```

### 7.4 Normalised Attribute Contract

The canonical fields each extractor must produce. Both state and cloud
extractors must use identical keys, types, and value representations.

**aws_instance**
```
instance_type                str     "t2.micro"
ami                          str     "ami-0abc…"
subnet_id                    str     "subnet-…"
key_name                     str | None
associate_public_ip_address  bool
monitoring                   bool    (detailed monitoring)
iam_instance_profile         str | None   (profile name, not ARN)
vpc_security_group_ids       list[str]    sorted alphabetically
ebs_optimized                bool
root_block_device_size       int     (GiB)
root_block_device_type       str     "gp3"
```

**aws_vpc**
```
cidr_block                   str     "10.0.0.0/16"
instance_tenancy             str     "default" | "dedicated"
enable_dns_support           bool
enable_dns_hostnames         bool
```

**aws_subnet**
```
vpc_id                       str
cidr_block                   str
availability_zone            str
map_public_ip_on_launch      bool
```

**aws_security_group**
```
name                         str
description                  str
vpc_id                       str
ingress_rules                list[SGRule]   sorted by (protocol, from_port, to_port)
egress_rules                 list[SGRule]   sorted by (protocol, from_port, to_port)
```

`SGRule` is a frozen dataclass:
```python
@dataclass(frozen=True)
class SGRule:
    protocol: str            # "tcp", "udp", "-1" (all)
    from_port: int
    to_port: int
    cidr_blocks: tuple[str, ...]       # sorted
    ipv6_cidr_blocks: tuple[str, ...]  # sorted
    source_sg_ids: tuple[str, ...]     # sorted
```

Rules are sorted so ordering differences between tfstate and the API
response never appear as drift.

**aws_s3_bucket**
```
versioning_enabled           bool    False if NoSuchVersioning
sse_algorithm                str | None   "aws:kms" | "AES256" | None
block_public_acls            bool
ignore_public_acls           bool
block_public_policy          bool
restrict_public_buckets      bool
logging_target_bucket        str | None
logging_target_prefix        str | None
```

### 7.5 Workspace

```python
@dataclass
class Workspace:
    id: str                  # UUID
    name: str                # "prod", "staging"
    provider: str            # "aws"
    state_backend: str       # "local" | "s3"
    state_path: str          # local: file path, s3: "s3://bucket/key"
    state_region: str | None # only for s3 backend
    region: str              # AWS region to scan
    detect_unmanaged: bool
    schedule_cron: str | None
    created_at: str
    last_scan_id: str | None
```

---

## 8. Component Specifications

### 8.1 State Reader (`driftctl/state/reader.py`)

Reads a Terraform state file from either a local path or an S3 URI and returns
the raw JSON content.

```python
def read_state(source: str, region: str | None = None) -> dict:
    """
    source: local file path OR "s3://bucket/key"
    Returns the parsed JSON dict of the tfstate file.
    Raises: StateReadError, UnsupportedStateVersionError
    """
```

**Local backend:**
```python
with open(source) as f:
    data = json.load(f)
```

**S3 backend:**
Detect `source.startswith("s3://")`. Parse bucket and key from the URI.
```python
session = boto3.Session(region_name=region)
s3 = session.client("s3")
obj = s3.get_object(Bucket=bucket, Key=key)
data = json.loads(obj["Body"].read())
```

**Validation (both backends):**
- Assert `data["version"] == 4`. Terraform state v4 is the format used by
  Terraform 0.12 and later. Raise `UnsupportedStateVersionError` if not.
- Iterate `data["resources"]`. Skip entries where `mode != "managed"` —
  data sources (`mode == "data"`) are not infrastructure.
- For each managed resource, iterate `instances[]` and yield
  `(type, name, attributes_dict)` tuples.

### 8.2 State Extractor (`driftctl/state/extractor.py`)

Converts raw tfstate attribute blocks into normalised `Resource` objects.

```python
def extract_from_state(
    resource_type: str,
    resource_name: str,
    attributes: dict
) -> Resource | None:
```

Uses a dispatch dict keyed by `resource_type`. If the type is not supported,
return `None` and log a warning — unknown types never crash the scan.

Each type handler maps tfstate field names to the canonical attribute contract
in Section 7.4, performing:
- snake_case passthrough (tfstate is already snake_case)
- Type coercions: `"true"`/`"false"` strings → `bool`
- Sorted lists: `vpc_security_group_ids`, SG rule lists
- Tag extraction: `attributes["tags"]` is already `{"key": "value"}` in tfstate

### 8.3 Cloud Provider Interface (`driftctl/providers/base.py`)

```python
from abc import ABC, abstractmethod

class CloudProvider(ABC):

    @abstractmethod
    def fetch(self, resource_type: str) -> list[Resource]:
        """
        Fetch all live resources of the given type.
        Handles pagination internally.
        Returns normalised Resource objects (source="actual").
        """

    @abstractmethod
    def supported_types(self) -> list[str]:
        """Return the resource types this provider can fetch."""
```

Registry (`driftctl/providers/registry.py`):
```python
def DefaultRegistry(region: str, profile: str | None = None) -> dict[str, CloudProvider]:
    return {
        "aws": AWSProvider(region=region, profile=profile)
    }
```

### 8.4 AWS Provider (`driftctl/providers/aws.py`)

Implements `CloudProvider` using boto3.

```python
class AWSProvider(CloudProvider):

    def __init__(self, region: str, profile: str | None = None):
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
        dispatch = {
            "aws_instance":       self._fetch_instances,
            "aws_vpc":            self._fetch_vpcs,
            "aws_subnet":         self._fetch_subnets,
            "aws_security_group": self._fetch_security_groups,
            "aws_s3_bucket":      self._fetch_s3_buckets,
        }
        return dispatch[resource_type]()
```

**Pagination:** EC2 fetchers use `get_paginator`:
```python
paginator = self._ec2.get_paginator("describe_instances")
for page in paginator.paginate():
    for reservation in page["Reservations"]:
        for instance in reservation["Instances"]:
            ...
```

**Normalisation (cloud-side):** Each `_fetch_*` method maps boto3 PascalCase
response keys to the same canonical attribute contract as the state extractor.
Tags in boto3 EC2 responses come as `[{"Key": "env", "Value": "prod"}]` and
must be converted to `{"env": "prod"}`.

**S3 multi-call assembly:**
```python
def _fetch_s3_buckets(self) -> list[Resource]:
    buckets = self._s3.list_buckets()["Buckets"]
    resources = []
    for bucket in buckets:
        name = bucket["Name"]
        attributes = {}
        tags = {}

        # versioning
        try:
            v = self._s3.get_bucket_versioning(Bucket=name)
            attributes["versioning_enabled"] = v.get("Status") == "Enabled"
        except Exception:
            attributes["versioning_enabled"] = False

        # encryption
        try:
            enc = self._s3.get_bucket_encryption(Bucket=name)
            rules = enc["ServerSideEncryptionConfiguration"]["Rules"]
            attributes["sse_algorithm"] = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        except Exception:
            attributes["sse_algorithm"] = None

        # tagging
        try:
            t = self._s3.get_bucket_tagging(Bucket=name)
            tags = {tag["Key"]: tag["Value"] for tag in t.get("TagSet", [])}
        except Exception:
            tags = {}

        # public access block
        try:
            pab = self._s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
            attributes["block_public_acls"]        = pab.get("BlockPublicAcls", False)
            attributes["ignore_public_acls"]       = pab.get("IgnorePublicAcls", False)
            attributes["block_public_policy"]      = pab.get("BlockPublicPolicy", False)
            attributes["restrict_public_buckets"]  = pab.get("RestrictPublicBuckets", False)
        except Exception:
            attributes.update({
                "block_public_acls": False, "ignore_public_acls": False,
                "block_public_policy": False, "restrict_public_buckets": False,
            })

        # logging
        try:
            log = self._s3.get_bucket_logging(Bucket=name).get("LoggingEnabled", {})
            attributes["logging_target_bucket"] = log.get("TargetBucket")
            attributes["logging_target_prefix"] = log.get("TargetPrefix")
        except Exception:
            attributes["logging_target_bucket"] = None
            attributes["logging_target_prefix"] = None

        resources.append(Resource(
            type="aws_s3_bucket", id=name, name=None,
            attributes=attributes, tags=tags, source="actual"
        ))
    return resources
```

### 8.5 Report Renderers (`driftctl/report/`)

**JSON renderer** — serialises a `ScanReport` to a stable JSON dict.
Used by: `--output json`, REST API response body, dashboard data source.

```json
{
  "scan_id": "uuid",
  "created_at": "2025-06-01T09:00:00Z",
  "state_path": "s3://my-bucket/prod/terraform.tfstate",
  "region": "us-east-1",
  "workspace": "prod",
  "summary": {
    "total_resources": 12,
    "drifted": 3,
    "missing": 1,
    "unmanaged": 1,
    "modified": 1,
    "tag_drift": 0
  },
  "results": [
    {
      "type": "aws_security_group",
      "id": "sg-0abc123",
      "name": "web_sg",
      "status": "MODIFIED",
      "attribute_diffs": {
        "ingress_rules": {
          "expected": ["..."],
          "actual": ["..."]
        }
      },
      "tag_diffs": {},
      "remediation": "terraform apply\n  # ingress_rules will revert to declared state"
    }
  ]
}
```

**Table renderer** — uses `rich.table.Table` for terminal output.
Status cells are colour-coded: MISSING=red, UNMANAGED=yellow, MODIFIED=cyan,
TAG_DRIFT=blue, IN_SYNC=green. Includes a `Remediation` column.

```
┌──────────────────────┬──────────────┬───────────┬────────────────────────────────────┐
│ Resource             │ ID           │ Status    │ Remediation                        │
├──────────────────────┼──────────────┼───────────┼────────────────────────────────────┤
│ aws_security_group   │ sg-0abc123   │ MODIFIED  │ terraform apply                    │
│ aws_instance         │ i-0def456    │ MISSING   │ terraform apply / state rm         │
│ aws_s3_bucket        │ my-bucket    │ UNMANAGED │ terraform import aws_s3_bucket...  │
└──────────────────────┴──────────────┴───────────┴────────────────────────────────────┘
  3 resources drifted (1 MISSING, 1 UNMANAGED, 1 MODIFIED)
```

---

## 9. Drift Engine

`driftctl/engine/drift.py` — pure function, no I/O, no AWS, no filesystem.

```python
def detect_drift(
    expected: list[Resource],
    actual: list[Resource],
    detect_unmanaged: bool = False,
) -> list[DriftResult]:
```

### Algorithm

**Step 1 — Index both sides**
```python
expected_index = {(r.type, r.id): r for r in expected}
actual_index   = {(r.type, r.id): r for r in actual}
```

**Step 2 — MISSING**
For every key in `expected_index` not in `actual_index`:
```python
DriftResult(type, id, name, status="MISSING",
            attribute_diffs={}, tag_diffs={}, remediation=...)
```

**Step 3 — UNMANAGED**
Only when `detect_unmanaged=True`.
For every key in `actual_index` not in `expected_index`:
```python
DriftResult(type, id, name=None, status="UNMANAGED",
            attribute_diffs={}, tag_diffs={}, remediation=...)
```

**Step 4 — Compare shared resources**
For every key present in both indexes:

```python
exp = expected_index[key]
act = actual_index[key]

attr_diffs = {
    field: {"expected": exp.attributes[field], "actual": act.attributes[field]}
    for field in exp.attributes
    if exp.attributes.get(field) != act.attributes.get(field)
}

tag_diffs = {}
all_tag_keys = set(exp.tags) | set(act.tags)
for k in all_tag_keys:
    if exp.tags.get(k) != act.tags.get(k):
        tag_diffs[k] = {"expected": exp.tags.get(k), "actual": act.tags.get(k)}

if not attr_diffs and not tag_diffs:
    status = "IN_SYNC"
elif attr_diffs:
    status = "MODIFIED"
else:
    status = "TAG_DRIFT"
```

**Step 5 — Return** the full list including IN_SYNC.
Renderers filter out IN_SYNC unless `--verbose` is passed.

### Comparison rules

- Scalar fields (`str`, `int`, `bool`): compared with `==`
- List fields (`vpc_security_group_ids`, SG rules): already sorted by extractors,
  compared with `==`
- `None` vs absent key: treat both as `None` to avoid false positives when one
  side omits a field entirely

---

## 10. Remediation Hints ★

`driftctl/engine/remediate.py` — pure function, no I/O.

Called after drift detection. Adds the `remediation` field to every `DriftResult`.

```python
def generate_remediation(result: DriftResult) -> str | None:
```

### Rules per status

**UNMANAGED**
The resource exists in AWS but Terraform doesn't know about it. The fix is to
import it into state.

```
terraform import <type>.<suggested_name> <id>

# Then add the corresponding HCL resource block to your configuration.
# Suggested name is a placeholder — rename to match your naming convention.
```

`suggested_name` is derived from the resource ID with the AWS prefix stripped:
`i-0abc1234` → `instance_i_0abc1234`, `sg-0def5678` → `sg_0def5678`.
For S3: bucket name → snake_case e.g. `my-prod-bucket` → `my_prod_bucket`.

**MISSING**
The resource is in state but gone from AWS. Two possible intentions:

```
# Option A — recreate (if deletion was unintentional):
terraform apply

# Option B — remove from state (if deletion was intentional):
terraform state rm '<type>.<name>'
```

`<name>` uses `result.name` if available (from the state side), otherwise `<id>`.

**MODIFIED**
The live resource attributes differ from the declared state. Fix by reverting:

```
terraform apply
  # The following attributes will revert to their declared values:
  #   <field>: "<actual>" → "<expected>"
  #   <field>: "<actual>" → "<expected>"
```

The field list is built from `result.attribute_diffs`. Each entry shows
`actual → expected` (the direction of the revert).

**TAG_DRIFT**
Same mechanism as MODIFIED but scoped to tags:

```
terraform apply
  # The following tags will be resynced:
  #   <tag_key>: "<actual>" → "<expected>"
```

**IN_SYNC**
Returns `None`. No remediation needed.

### Where remediation appears

- CLI `--output table` — `Remediation` column (truncated to 60 chars, full text
  on drill-down)
- CLI `--output json` — `"remediation"` field on each result
- REST API `GET /api/v1/scans/{id}/report` — included in the JSON response
- Dashboard per-resource drill-down — full text, copy button ★

---

## 11. CLI

Built with **Typer**. Installed as the `driftctl` command via
`pyproject.toml` entry points.

### `driftctl scan`

```
driftctl scan [OPTIONS]

Compare a Terraform state file against live AWS infrastructure.

Options:
  --state TEXT          State source: local path OR s3://bucket/key
                        [required unless --config + --workspace]
  --config PATH         Path to driftctl.yaml
                        [default: configs/driftctl.yaml]
  --workspace TEXT      Workspace name defined in config file
  --provider TEXT       Cloud provider  [default: aws]
  --region TEXT         AWS region  [default: us-east-1]
  --profile TEXT        AWS credential profile (optional)
  --output [table|json] Output format  [default: table]
  --skip-cloud          Parse state only, skip AWS API calls.
                        Useful for offline testing and validating
                        the state file structure.
  --unmanaged           Detect resources in cloud not in state
                        (off by default to reduce noise)
  --verbose             Include IN_SYNC resources in output
  --output-file PATH    Write JSON output to file
  --help
```

**Exit codes:**
- `0` — no drift detected
- `1` — drift detected
- `2` — error (state parse failure, AWS credential error, etc.)

### `driftctl report`

```
driftctl report SCAN_ID [OPTIONS]

Display a saved scan report from the database.

Options:
  --output [table|json]   [default: table]
  --output-file PATH
```

### `driftctl scans list`

```
driftctl scans list [OPTIONS]

List recent scans stored in the database.

Options:
  --workspace TEXT      Filter by workspace name
  --limit INTEGER       [default: 20]
  --output [table|json] [default: table]
```

### `driftctl workspace list`

```
driftctl workspace list

List all workspaces defined in the database.
```

### `driftctl workspace create`

```
driftctl workspace create [OPTIONS]

Options:
  --name TEXT           Workspace name  [required]
  --state TEXT          State source (local path or s3://…)  [required]
  --region TEXT         AWS region  [required]
  --backend [local|s3]  [default: local]
  --cron TEXT           Cron expression for scheduled scans
  --unmanaged           Enable unmanaged resource detection
```

### `driftctl schedule create`

```
driftctl schedule create [OPTIONS]

Set or update a cron schedule for a workspace.

Options:
  --workspace TEXT      Workspace name  [required]
  --cron TEXT           Cron expression  [required]
                        Example: "0 6 * * *"  (daily at 06:00 UTC)
                        Example: "0 */6 * * *"  (every 6 hours)
```

### `driftctl serve`

```
driftctl serve [OPTIONS]

Start the REST API server and web dashboard.

Options:
  --host TEXT    [default: 0.0.0.0]
  --port INT     [default: 8080]
  --reload       Enable auto-reload (development)
```

---

## 12. REST API

Built with **FastAPI**, served by `uvicorn`.
Start: `driftctl serve` or `make run-server`.
Open: `http://localhost:8080`

### Authentication
When `api.api_key` is set in `driftctl.yaml`, all API endpoints (except
`/health`) require the header `X-API-Key: <value>`. Implemented as FastAPI
middleware. When `api_key` is empty, no auth is required.

### Endpoints

#### `GET /health`
Health check. No auth required.
```json
{"status": "ok", "version": "1.0.0"}
```

#### `GET /api/v1/workspaces`
List all workspaces.
```json
[{"id": "uuid", "name": "prod", "provider": "aws", ...}]
```

#### `POST /api/v1/workspaces`
Create a workspace.
Request body:
```json
{
  "name": "prod",
  "provider": "aws",
  "state_backend": "s3",
  "state_path": "s3://my-tf-state/prod/terraform.tfstate",
  "state_region": "us-east-1",
  "region": "us-east-1",
  "detect_unmanaged": false,
  "schedule_cron": "0 6 * * *"
}
```
Response: `201 Created` with the created workspace object.

#### `POST /api/v1/workspaces/{id}/scans`
Trigger an on-demand scan for a workspace.
Response: `202 Accepted`
```json
{"scan_id": "uuid", "status": "running"}
```
The scan runs in a background task. Poll
`GET /api/v1/scans/{scan_id}` for completion.

#### `GET /api/v1/scans`
List recent scans, newest first.
Query params: `workspace` (filter by name), `limit` (default 20).
```json
[{
  "scan_id": "uuid",
  "created_at": "...",
  "workspace": "prod",
  "status": "complete",
  "drifted_count": 3,
  "exit_code": 1
}]
```

#### `GET /api/v1/scans/{id}/report`
Get the full drift report for a completed scan.
Query param: `format=json` (default) or `format=table` (plain text).
Returns the full `ScanReport` JSON including `remediation` fields.

#### `PUT /api/v1/workspaces/{id}/schedules`
Set or update the cron schedule for a workspace.
```json
{"cron": "0 */6 * * *"}
```
Response: `200 OK`. Immediately updates the APScheduler job.

### Response envelope
All responses use:
```json
{"data": {...}, "error": null}
```
Errors:
```json
{"data": null, "error": {"code": "NOT_FOUND", "message": "Scan not found"}}
```

---

## 13. Web Dashboard ★

A single-page HTML + vanilla JavaScript application served by FastAPI at `GET /`.
No build step. No npm. No webpack. One `index.html` file.

Abhishek's original project has a basic dashboard showing the current scan.
This extended dashboard adds scan history, drill-down, and trends.

### View 1 — Dashboard Home (`/`)

Displays:
- **Workspace cards** — one card per workspace showing: name, last scan
  timestamp, drifted resource count, scan status (running / complete / error),
  and a "Scan Now" button that calls `POST /api/v1/workspaces/{id}/scans` and
  shows an inline spinner until the scan completes.
- **Recent scans table** — last 20 scans across all workspaces: timestamp,
  workspace, drifted count, status. Each row links to the scan detail view.
- **Summary counts** — total MISSING / UNMANAGED / MODIFIED / TAG_DRIFT across
  the most recent scan per workspace.

### View 2 — Scan Detail (`/scans/{id}`)  ★

The per-resource drill-down. This is your primary addition to the dashboard.

Displays:
- Scan metadata: ID, timestamp, workspace, state path, region, summary counts.
- **Drift results table**: type, ID, name, status (colour-coded).
- **Expandable rows**: click any row to expand a detail panel showing:
  - Full `attribute_diffs` table: field name | expected value | actual value
  - Full `tag_diffs` table: tag key | expected | actual
  - **Remediation command** in a code block with a copy-to-clipboard button ★
- Filter controls: filter by status (MISSING / UNMANAGED / MODIFIED / TAG_DRIFT)
- JSON download button: calls `/api/v1/scans/{id}/report?format=json`

### View 3 — Drift Trends (`/trends`)  ★

A timeline showing drift count over time, built from the SQLite scan history.

Displays:
- **SVG line chart** — one line per workspace. X-axis = scan date, Y-axis =
  drifted resource count. Built with pure SVG (no charting library).
- **Time range selector** — last 7 days / 30 days / 90 days.
- **Per-workspace toggle** — show/hide individual workspace lines.
- Auto-refreshes every 60 seconds.

Data source: `GET /api/v1/scans?limit=200` filtered and grouped by workspace
in the client.

### Polling
While a scan is in `running` state (after clicking "Scan Now"), the dashboard
polls `GET /api/v1/scans/{id}` every 3 seconds. On `complete`, it refreshes
the scan detail view automatically.

### Technical constraints
- All interactivity via vanilla JS `fetch()` calls to the REST API.
- No external JS libraries loaded at runtime (no jQuery, no Chart.js).
- SVG chart rendered by a ~100-line pure JS function — no dependencies.
- Works without an internet connection once the server is running.

---

## 14. Cron Scheduler

`driftctl/scheduler/jobs.py`

APScheduler `BackgroundScheduler` runs inside the FastAPI server process.
No separate worker. No Redis. No Celery. Single container deployment.

### Lifecycle

**On server start** (`driftctl serve`):
```python
scheduler = BackgroundScheduler()
workspaces = db.list_workspaces()
for ws in workspaces:
    if ws.schedule_cron:
        register_job(scheduler, ws)
scheduler.start()
```

**Job function:**
```python
def trigger_scan(workspace_id: str):
    # Runs the full scan pipeline for a workspace
    # Saves result to SQLite
    # Identical to POST /api/v1/workspaces/{id}/scans
```

**When a schedule is updated** (`PUT /api/v1/workspaces/{id}/schedules`):
```python
scheduler.remove_job(f"workspace_{workspace_id}")
register_job(scheduler, updated_workspace)
```

**When a workspace is deleted:**
```python
scheduler.remove_job(f"workspace_{workspace_id}")
```

### Cron format
Standard 5-field Unix cron: `minute hour day_of_month month day_of_week`.
```
"0 6 * * *"      # daily at 06:00 UTC
"0 */6 * * *"    # every 6 hours
"0 9 * * 1"      # every Monday at 09:00 UTC
"*/30 * * * *"   # every 30 minutes
```

---

## 15. SQLite Persistence

File: `driftctl.db` (configurable via `database:` key in `driftctl.yaml`).
Uses Python stdlib `sqlite3`. Schema is created on first run.

```sql
-- Workspaces
CREATE TABLE IF NOT EXISTS workspaces (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    provider          TEXT NOT NULL DEFAULT 'aws',
    state_backend     TEXT NOT NULL DEFAULT 'local',
    state_path        TEXT NOT NULL,
    state_region      TEXT,
    region            TEXT NOT NULL,
    detect_unmanaged  INTEGER NOT NULL DEFAULT 0,
    schedule_cron     TEXT,
    created_at        TEXT NOT NULL,
    last_scan_id      TEXT
);

-- Scans
CREATE TABLE IF NOT EXISTS scans (
    id                TEXT PRIMARY KEY,
    workspace_id      TEXT,
    created_at        TEXT NOT NULL,
    state_path        TEXT NOT NULL,
    region            TEXT NOT NULL,
    status            TEXT NOT NULL,  -- pending | running | complete | error
    drifted_count     INTEGER,
    total_resources   INTEGER,
    exit_code         INTEGER,
    error_message     TEXT,
    summary_json      TEXT,           -- serialised summary dict
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

-- Drift results (one row per resource per scan)
CREATE TABLE IF NOT EXISTS drift_results (
    id                TEXT PRIMARY KEY,
    scan_id           TEXT NOT NULL,
    type              TEXT NOT NULL,
    resource_id       TEXT NOT NULL,
    resource_name     TEXT,
    status            TEXT NOT NULL,
    attribute_diffs   TEXT NOT NULL,  -- JSON
    tag_diffs         TEXT NOT NULL,  -- JSON
    remediation       TEXT,           -- ★ YOUR ADDITION
    FOREIGN KEY (scan_id) REFERENCES scans(id)
);

-- Indexes for dashboard queries
CREATE INDEX IF NOT EXISTS idx_scans_workspace ON scans(workspace_id);
CREATE INDEX IF NOT EXISTS idx_scans_created   ON scans(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_results_scan    ON drift_results(scan_id);
```

---

## 16. Configuration

`configs/driftctl.yaml` — optional. CLI flags always override file values.

```yaml
# SQLite database path
database: driftctl.db

# REST API settings
api:
  addr: ":8080"
  api_key: ""            # set to enable X-API-Key auth; leave empty to disable

# Default scan settings (overridden per-workspace or by CLI flag)
default_region: us-east-1
default_profile: ""      # AWS named profile; empty = use default credential chain

scan:
  detect_unmanaged: false

# Workspaces
workspaces:
  - name: prod
    provider: aws
    state:
      backend: s3
      bucket: my-tf-state
      key: prod/terraform.tfstate
      region: us-east-1
    regions: [us-east-1]
    schedule:
      cron: "0 */6 * * *"

  - name: local-dev
    provider: aws
    state:
      backend: local
      path: ./terraform.tfstate
    regions: [us-east-1]
    schedule:
      cron: "0 9 * * 1"
```

### Credential precedence (AWS)
driftctl never manages AWS credentials directly. It uses the standard boto3
credential chain in this order:
1. `--profile` CLI flag (or `default_profile` in config)
2. `AWS_PROFILE` environment variable
3. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables
4. `~/.aws/credentials` file
5. IAM instance profile (when running on EC2)

---

## 17. Project Layout

```
driftctl/
├── README.md
├── Makefile
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .pre-commit-config.yaml
│
├── .github/
│   └── workflows/
│       ├── ci.yml               # ruff + mypy + pytest on every push
│       └── release.yml          # PyPI publish on version tag
│
├── configs/
│   └── driftctl.yaml            # example config (local + S3 workspaces)
│
├── testdata/
│   └── sample.tfstate           # hand-crafted state with all 5 resource
│                                # types, including deliberate drift examples
│
├── driftctl/
│   ├── __init__.py
│   ├── cli.py                   # Typer app: scan, report, scans, workspace,
│   │                            #            schedule, serve
│   ├── config.py                # YAML loading, env var merging, Config model
│   ├── models.py                # Resource, DriftResult, ScanReport,
│   │                            # SGRule, Workspace
│   │
│   ├── state/
│   │   ├── __init__.py
│   │   ├── reader.py            # read_state(): local + S3 backend
│   │   └── extractor.py        # extract_from_state(): tfstate → Resource
│   │
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py              # CloudProvider ABC
│   │   ├── aws.py               # AWSProvider: boto3 fetch + normalise
│   │   └── registry.py         # DefaultRegistry()
│   │
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── drift.py             # detect_drift(): pure function
│   │   └── remediate.py        # generate_remediation(): pure function ★
│   │
│   ├── report/
│   │   ├── __init__.py
│   │   ├── json_renderer.py     # ScanReport → JSON dict
│   │   └── table_renderer.py   # ScanReport → rich Table
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── server.py            # FastAPI app factory, lifespan hook
│   │   ├── middleware.py        # X-API-Key auth middleware
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── health.py        # GET /health
│   │   │   ├── workspaces.py   # workspace CRUD + scan trigger
│   │   │   └── scans.py        # scan list + report
│   │   └── static/
│   │       └── index.html       # single-file web dashboard ★
│   │
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── jobs.py              # APScheduler setup + job registration
│   │
│   └── storage/
│       ├── __init__.py
│       └── db.py                # SQLite schema init + CRUD functions
│
└── tests/
    ├── conftest.py              # fixtures: Resources, ScanReport, tmp db path
    ├── test_state_reader.py     # local + S3 (moto s3 mock)
    ├── test_state_extractor.py  # per-type normalisation
    ├── test_aws_provider.py     # boto3 fetch + normalise (moto ec2/s3)
    ├── test_drift_engine.py     # all 5 statuses, edge cases
    ├── test_remediation.py      # exact command strings per status ★
    ├── test_api.py              # all 7 endpoints (FastAPI TestClient)
    └── test_smoke.py            # e2e: --skip-cloud on sample.tfstate
```

---

## 18. Testing Strategy

**Goal: full test suite runs offline, no real AWS, under 60 seconds.**

### `test_state_reader.py`
- Parse `testdata/sample.tfstate` — assert correct resource count
- Assert unknown version raises `UnsupportedStateVersionError`
- Assert `mode == "data"` resources are skipped
- S3 backend: use `moto` to mock S3, upload a tfstate object, assert reader
  downloads and parses it correctly

### `test_state_extractor.py`
- For each of the 5 resource types: provide a raw tfstate attributes block
  and assert the output `Resource` has the correct field values
- `vpc_security_group_ids` is sorted regardless of input order
- SG rules are sorted by `(protocol, from_port, to_port)`
- S3 tags extracted correctly from `attributes["tags"]`
- Unknown type returns `None` without raising

### `test_aws_provider.py`
- Use `moto` to mock EC2 and S3 APIs
- Create a real resource in the mock, call the provider, assert the returned
  `Resource` has the same normalised fields the state extractor would produce
- This is the critical parity test: identical resource → both extractors →
  identical `Resource` → drift engine → `IN_SYNC`
- S3: assert all five per-bucket calls are assembled correctly
- S3: assert `NoSuch*` exceptions are caught and fields set to `None`/`False`

### `test_drift_engine.py`
- `MISSING`: expected has resource, actual does not
- `UNMANAGED`: actual has resource, expected does not (with `detect_unmanaged=True`)
- `UNMANAGED` suppressed when `detect_unmanaged=False`
- `MODIFIED`: same id, one field differs
- `TAG_DRIFT`: same id, same attributes, one tag differs
- `IN_SYNC`: identical resource on both sides
- Multiple resources: mix of all statuses in one call
- Empty expected + populated actual → all UNMANAGED
- Populated expected + empty actual → all MISSING

### `test_remediation.py` ★
- `MISSING` with known name → assert `terraform state rm 'aws_instance.web'` in output
- `MISSING` without name → uses resource id
- `UNMANAGED` → assert `terraform import aws_instance.` prefix in output
- `MODIFIED` with two changed fields → assert both field names appear in output
  with correct expected/actual direction
- `TAG_DRIFT` → assert tag key appears in output
- `IN_SYNC` → returns `None`

### `test_api.py`
- Use `FastAPI TestClient` with a temporary SQLite file
- `GET /health` → 200
- `POST /api/v1/workspaces` → 201, workspace in db
- `GET /api/v1/workspaces` → list includes created workspace
- `POST /api/v1/workspaces/{id}/scans` → 202 (inject pre-built ScanReport,
  skip real AWS)
- `GET /api/v1/scans` → list includes scan
- `GET /api/v1/scans/{id}/report` → full JSON with remediation fields
- `PUT /api/v1/workspaces/{id}/schedules` → 200, cron updated in db
- Auth: request without `X-API-Key` when key is set → 401

### `test_smoke.py`
End-to-end, no AWS:
```bash
driftctl scan \
  --state testdata/sample.tfstate \
  --skip-cloud \
  --output json \
  --output-file /tmp/result.json
```
- Assert exit code `0`
- Assert `/tmp/result.json` is valid JSON
- Assert `summary` object is present
- Assert all results have `remediation` field (not null for non-IN_SYNC)

---

## 19. Packaging & Developer Experience

### Makefile
```makefile
make build         # pip install -e ".[dev]"
make test          # pytest tests/ -v --cov=driftctl
make lint          # ruff check . && ruff format --check . && mypy driftctl/
make run-server    # uvicorn driftctl.api.server:app --reload --port 8080
make docker-build  # docker build -t driftctl:latest .
make docker-run    # docker run -p 8080:8080 -v $(PWD):/data driftctl:latest serve
make clean         # remove __pycache__, .mypy_cache, dist/
```

### Dockerfile
```dockerfile
FROM python:3.11-slim
RUN useradd -m driftctl
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
USER driftctl
EXPOSE 8080
HEALTHCHECK CMD curl -f http://localhost:8080/health || exit 1
ENTRYPOINT ["driftctl"]
CMD ["serve"]
```

### pyproject.toml structure
```toml
[project]
name = "driftctl"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "boto3",
    "typer",
    "rich",
    "pyyaml",
    "fastapi",
    "uvicorn",
    "apscheduler",
]

[project.scripts]
driftctl = "driftctl.cli:app"

[project.optional-dependencies]
dev = [
    "ruff",
    "mypy",
    "pytest",
    "pytest-cov",
    "moto[ec2,s3]",
    "httpx",
    "pre-commit",
]
```

### CI pipeline (`.github/workflows/ci.yml`)
Runs on every push and pull request:
```
ruff check .
ruff format --check .
mypy driftctl/
pytest tests/ --cov=driftctl --cov-report=xml
```
Coverage report uploaded to Codecov. Coverage badge in README.

### README structure
1. One-line description + CI / coverage / PyPI badges
2. Short GIF or asciinema demo showing a scan finding drift
3. "What it detects" (4 drift types, one sentence each)
4. Quick start: pip install, scan command, server command
5. CLI reference
6. Configuration file reference
7. REST API table (mirrors Section 12)
8. Dashboard screenshots ★
9. Contributing / dev setup

---

## 20. Build Phases

Each phase ends with all tests green and the tool usable in its current state.

| Phase | Deliverable | What you can do at the end |
|---|---|---|
| **1 — State Reader** | `state/reader.py` (local + S3), `state/extractor.py` for all 5 types, `testdata/sample.tfstate`, unit tests | Parse any local or S3 tfstate and print the extracted resources |
| **2 — Cloud Fetcher** | `providers/aws.py` for all 5 types, moto tests, parity tests | Fetch and normalise live AWS resources in the same shape as the state extractor |
| **3 — Drift Engine + Remediation** | `engine/drift.py`, `engine/remediate.py` ★, JSON + table renderers, unit tests | Run a full end-to-end scan with `--skip-cloud`; all drift results show remediation hints |
| **4 — CLI** | `cli.py` with all commands, exit codes, `--skip-cloud`, `--output`, `--unmanaged` | `driftctl scan --state ./terraform.tfstate --region us-east-1` works live |
| **5 — Persistence + Config** | `storage/db.py`, `config.py`, YAML loading, `driftctl report`, `driftctl scans list` | Scans are saved; past reports can be retrieved; YAML workspaces work |
| **6 — REST API + Scheduler** | `api/` with all 7 endpoints, `scheduler/jobs.py`, `driftctl serve` | `make run-server` starts the server; API endpoints respond; cron jobs fire |
| **7 — Basic Dashboard** | `api/static/index.html` — workspace cards + recent scans (mirrors Abhishek's original) | Browse to `http://localhost:8080` and see scan results |
| **8 — Enhanced Dashboard** ★ | Scan history view, per-resource drill-down, field diffs, copy remediation button, drift trends chart | Click any drifted resource to see exactly what changed and the fix command |

Phases 1–7 = Abhishek's full feature set, in Python.
Phase 8 = your contribution alongside the remediation hints from Phase 3.

---

## 21. Dependencies

### Runtime
| Package | Purpose | Used from phase |
|---|---|---|
| `boto3` | AWS API calls (EC2, S3) + S3 state backend | 1 |
| `typer` | CLI framework | 4 |
| `rich` | Table rendering, colour output | 3 |
| `pyyaml` | Config file parsing | 5 |
| `fastapi` | REST API framework | 6 |
| `uvicorn` | ASGI server | 6 |
| `apscheduler` | Cron scheduler | 6 |

### Development / CI
| Package | Purpose |
|---|---|
| `ruff` | Linter + formatter |
| `mypy` | Static type checker |
| `pytest` | Test runner |
| `pytest-cov` | Coverage reporting |
| `moto[ec2,s3]` | AWS API mocking (offline tests) |
| `httpx` | Required by FastAPI `TestClient` |
| `pre-commit` | Local git hook runner |

### Standard library (no install needed)
`sqlite3`, `json`, `uuid`, `datetime`, `pathlib`, `abc`,
`dataclasses`, `typing`, `os`, `subprocess`

---

*Spec complete. Implementation begins at Phase 1.*
*Phases 1–7 reproduce Abhishek's feature set in Python.*
*Phase 3 (remediation) and Phase 8 (enhanced dashboard) are your additions.*
