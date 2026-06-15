# driftctl — Complete Technical Specification
**Version:** v1.0 (Final)
**Language:** Python 3.11+
**Foundation:** Based on Abhishek Veeramalla's terraform-drift-detector (Go)
**Original additions:** Remediation Hints ★ + Enhanced Web Dashboard ★
**Live demo:** https://driftctl.onrender.com
**Repository:** https://github.com/LeenaAdapakala17/terraform-drift-detector
**Tests:** 235 passing

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [How Drift Detection Works](#2-how-drift-detection-works)
3. [What This Project Detects](#3-what-this-project-detects)
4. [Feature Set](#4-feature-set)
5. [System Architecture](#5-system-architecture)
6. [Supported AWS Resources](#6-supported-aws-resources)
7. [Data Models](#7-data-models)
8. [Component Specifications](#8-component-specifications)
9. [Drift Engine](#9-drift-engine)
10. [Remediation Hints ★](#10-remediation-hints-)
11. [CLI](#11-cli)
12. [REST API](#12-rest-api)
13. [Web Dashboard ★](#13-web-dashboard-)
14. [Cron Scheduler](#14-cron-scheduler)
15. [SQLite Persistence](#15-sqlite-persistence)
16. [Configuration](#16-configuration)
17. [Project Layout](#17-project-layout)
18. [Testing Strategy](#18-testing-strategy)
19. [Packaging & Deployment](#19-packaging--deployment)
20. [Build Phases](#20-build-phases)
21. [Dependencies](#21-dependencies)

★ = original addition on top of Abhishek's foundation

---

## 1. Project Overview

driftctl compares Terraform state files against live AWS infrastructure to detect
configuration drift — without running `terraform plan` or `terraform apply`.

This project is a Python port of Abhishek Veeramalla's terraform-drift-detector
(originally written in Go), with two original additions built on top:

**Addition 1 — Remediation Hints ★**
For every drift result, the tool emits the exact Terraform command an engineer
should run to reconcile it. The original detects drift and stops. This addition
closes the loop to action.

**Addition 2 — Enhanced Web Dashboard ★**
Scan history view, per-resource drill-down with side-by-side field diffs
(Expected in green, Actual in red), copy-to-clipboard remediation command,
and a drift trends SVG chart built from SQLite scan history.

Everything else — state reader, cloud fetcher, drift engine, CLI, REST API,
scheduler, SQLite persistence, YAML config — mirrors Abhishek's original feature
set, ported to Python.

---

## 2. How Drift Detection Works

### The problem with terraform plan
The standard way to catch drift is `terraform plan`. It has three problems:

1. **Requires running Terraform** — credentials, right backend, right workspace
2. **Compares HCL to state, not state to cloud** — doesn't independently verify
   live AWS matches the state file
3. **Nobody runs it consistently** — drift accumulates silently between runs

### What driftctl does differently
Directly compares two things:
```
What Terraform thinks exists  ←── .tfstate file (expected model)
What AWS says actually exists ←── live boto3 API calls (actual model)
```
No Terraform binary. No apply risk. No side effects. Just reads.

### The normalisation challenge
The same resource looks different depending on where you read it:
- tfstate uses `instance_type` (snake_case). boto3 returns `InstanceType` (PascalCase)
- Security group rules ordered differently between tfstate and the API
- S3 config spread across five separate API calls in AWS

Both extractors must produce identical output for the same resource or everything
looks like drift. The parity tests in `test_aws_provider.py` prove they do.

---

## 3. What This Project Detects

**MISSING** — in state, gone from cloud (deleted out-of-band)
The resource exists in `.tfstate` but is gone from AWS. The next `terraform apply`
will attempt to recreate it.

**UNMANAGED** — in cloud, not in state (created out-of-band)
The resource exists live in AWS but has no Terraform lifecycle — no plan, no
destroy, no state lock.

**MODIFIED** — attributes changed out-of-band
The resource exists in both but one or more attributes differ. The live
configuration no longer matches the Terraform-declared intent.
Example: `instance_type` in state is `t2.micro` but live instance is `t3.small`.

**TAG_DRIFT** — only tags changed
Tags are tracked separately because they change more frequently, have their own
ops significance (cost allocation, ownership), and teams often want different
alerting policies for tag drift vs structural drift.

---

## 4. Feature Set

### From Abhishek's original (ported to Python)
- Terraform state reading from **local file** and **S3 bucket**
- Live AWS resource fetching for EC2, VPC, Subnet, Security Group, S3
- Normalised resource model with field-level comparison
- Four drift classifications: MISSING, UNMANAGED, MODIFIED, TAG_DRIFT
- CLI: `scan`, `report`, `scans list`, `workspace`, `schedule`, `serve`
- Output formats: JSON and Rich terminal table
- `--skip-cloud` mode for offline/CI state validation
- REST API with 7 endpoints (FastAPI)
- Basic web dashboard
- SQLite persistence (scans, results, workspaces, schedules)
- YAML configuration with workspace definitions
- Cron-based scheduled scanning (APScheduler)
- Optional API key authentication (X-API-Key header)
- Exit codes: 0 = no drift, 1 = drift detected, 2 = error

### Original additions ★
- **Remediation hints** — exact Terraform command per drift result, in CLI table,
  JSON output, REST API response, and dashboard drill-down
- **Enhanced dashboard** — scan history, per-resource drill-down, field diffs,
  copy-to-clipboard remediation, drift trends SVG chart

---

## 5. System Architecture

```
Local .tfstate / S3 URI
         │
         ▼
┌─────────────────────┐
│    State Reader      │  local: open() + json.load()
│                      │  s3:    boto3 s3.get_object()
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│   State Extractor   │  per-type dispatch
│   tfstate→Resource  │  snake_case, typed, sorted
└──────────┬──────────┘
           │ Expected Model: list[Resource]
           ▼
┌──────────────────────────────┐       ┌─────────────────────┐
│       Drift Engine           │◀──────│   Cloud Fetcher      │
│  index → diff → classify     │       │   AWS Provider       │
│  MISSING/UNMANAGED/MODIFIED  │       │   boto3 paginated    │
│  TAG_DRIFT/IN_SYNC           │       │   EC2, VPC, SG, S3   │
└──────────┬───────────────────┘       └─────────────────────┘
           │                                     │
           │                           Actual Model: list[Resource]
           ▼
┌─────────────────────┐ ★
│ Remediation Engine  │  UNMANAGED → terraform import
│                     │  MISSING   → terraform apply / state rm
│                     │  MODIFIED  → terraform apply + field list
│                     │  TAG_DRIFT → terraform apply + tag list
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│     ScanReport      │
│  list[DriftResult]  │
│  each with          │
│  remediation ★      │
└───┬─────────────────┘
    │
    ├──────────────┐──────────────┐
    ▼              ▼              ▼
┌────────┐  ┌──────────┐  ┌──────────────┐
│  CLI   │  │ REST API │  │   SQLite DB  │
│ table/ │  │ FastAPI  │  │  driftctl.db │
│ json   │  │ 7 endpoints│ └──────────────┘
└────────┘  └──────┬───┘
                   │
                   ▼
         ┌──────────────────┐ ★
         │  Web Dashboard   │
         │  • Scan history  │
         │  • Drill-down    │
         │  • Trends chart  │
         └──────────────────┘
```

---

## 6. Supported AWS Resources

All boto3 calls are read-only. AWS does not bill for `describe_*`/`get_*`/`list_*`.

| Terraform type | boto3 calls | Match key |
|---|---|---|
| `aws_instance` | `ec2.describe_instances` (paginated) | instance id `i-…` |
| `aws_vpc` | `ec2.describe_vpcs` + `describe_vpc_attribute` | vpc id `vpc-…` |
| `aws_subnet` | `ec2.describe_subnets` (paginated) | subnet id `subnet-…` |
| `aws_security_group` | `ec2.describe_security_groups` (paginated) | group id `sg-…` |
| `aws_s3_bucket` | `s3.list_buckets` + 5 per-bucket calls | bucket name |

**S3 per-bucket calls:** `get_bucket_versioning`, `get_bucket_encryption`,
`get_bucket_tagging`, `get_public_access_block`, `get_bucket_logging`.
Each can raise `NoSuch*` — treated as "feature disabled".

---

## 7. Data Models

### Resource
```python
@dataclass
class Resource:
    type: str          # "aws_instance", "aws_vpc", etc.
    id: str            # AWS resource ID or bucket name
    name: str | None   # Terraform logical name (expected side only)
    attributes: dict   # Normalised, comparable attributes
    tags: dict         # {"env": "prod", "Owner": "team"}
    source: str        # "expected" | "actual"
```

### DriftResult
```python
@dataclass
class DriftResult:
    type: str
    id: str
    name: str | None
    status: str          # MISSING|UNMANAGED|MODIFIED|TAG_DRIFT|IN_SYNC
    attribute_diffs: dict  # {field: {"expected": x, "actual": y}}
    tag_diffs: dict        # {key: {"expected": x, "actual": y}}
    remediation: str | None  # ★ Exact terraform command
```

### ScanReport
```python
@dataclass
class ScanReport:
    scan_id: str
    created_at: str      # ISO-8601
    state_path: str      # local path or s3://bucket/key
    region: str
    workspace: str | None
    results: list[DriftResult]
    # computed: total_resources, drifted_count, missing_count,
    #           unmanaged_count, modified_count, tag_drift_count, exit_code
```

### Normalised Attribute Contract (per resource type)

**aws_instance:** `instance_type`, `ami`, `subnet_id`, `key_name`,
`associate_public_ip_address`, `monitoring`, `iam_instance_profile`,
`vpc_security_group_ids` (sorted), `ebs_optimized`, `root_block_device_size`,
`root_block_device_type`

**aws_vpc:** `cidr_block`, `instance_tenancy`, `enable_dns_support`,
`enable_dns_hostnames`

**aws_subnet:** `vpc_id`, `cidr_block`, `availability_zone`,
`map_public_ip_on_launch`

**aws_security_group:** `name`, `description`, `vpc_id`,
`ingress_rules` (sorted SGRule list), `egress_rules` (sorted SGRule list)

**aws_s3_bucket:** `versioning_enabled`, `sse_algorithm`, `block_public_acls`,
`ignore_public_acls`, `block_public_policy`, `restrict_public_buckets`,
`logging_target_bucket`, `logging_target_prefix`

---

## 8. Component Specifications

### State Reader (`driftctl/state/reader.py`)
- `read_state(source, region=None) -> list[dict]`
- **Local:** `open(path)` + `json.load()`
- **S3:** `boto3.Session().client("s3").get_object(Bucket, Key)`
- Validates `version == 4` (Terraform 0.12+)
- Skips `mode != "managed"` (data sources excluded)
- Raises `StateReadError`, `UnsupportedStateVersionError`

### State Extractor (`driftctl/state/extractor.py`)
- `extract_from_state(type, name, attributes) -> Resource | None`
- Per-type dispatch table
- `_to_bool()` handles `"true"`/`"false"` string coercion
- SG rules normalised to sorted `SGRule` frozen dataclasses
- Unknown types return `None` gracefully

### Cloud Provider (`driftctl/providers/`)
- `CloudProvider` ABC: `fetch(resource_type)`, `supported_types()`
- `AWSProvider` implements with boto3 pagination
- `DefaultRegistry(region, profile)` → `{"aws": AWSProvider(...)}`
- Tags normalised from `[{"Key":…,"Value":…}]` to `{"key": "value"}`
- SG rules normalised from `IpPermissions` to sorted `SGRule` list
- S3: assembles config from 5 separate API calls per bucket

### Drift Engine (`driftctl/engine/drift.py`)
- `detect_drift(expected, actual, detect_unmanaged=False) -> list[DriftResult]`
- Pure function — no I/O, no AWS, no filesystem
- Index both sides by `(type, id)`
- MISSING: in expected, not in actual
- UNMANAGED: in actual, not in expected (only if flag True)
- Compare shared: field-by-field diff → MODIFIED / TAG_DRIFT / IN_SYNC
- `remediation` field is `None` at this stage — set by `remediate.py`

### Remediation Generator (`driftctl/engine/remediate.py`) ★
- `generate_remediation(result) -> str | None`
- `enrich_results(results) -> list[DriftResult]`
- Pure function — no I/O
- Per-status command templates with field-level details
- SGRule dataclasses rendered as clean readable dicts
- See Section 10 for full specification

---

## 9. Drift Engine

### Algorithm
```
Step 1: Index both sides by (type, id)
Step 2: MISSING  — key in expected, not in actual
Step 3: UNMANAGED — key in actual, not in expected (if detect_unmanaged=True)
Step 4: Compare shared resources
        attr_diffs = {field: {expected, actual}} for differing fields
        tag_diffs  = {key: {expected, actual}}   for differing tags
        if both empty → IN_SYNC
        elif attr_diffs → MODIFIED
        else → TAG_DRIFT
Step 5: Return all results (IN_SYNC filtered by renderer unless --verbose)
```

### Comparison rules
- Scalars (`str`, `int`, `bool`): `==`
- Lists (already sorted by extractors): `==`
- `None` vs absent key: both treated as `None`

---

## 10. Remediation Hints ★

`driftctl/engine/remediate.py` — your original addition.

### MISSING
```
# Option A — recreate (if deletion was unintentional):
terraform apply

# Option B — remove from state (if deletion was intentional):
terraform state rm 'aws_instance.web_server'
```

### UNMANAGED
```
# Bring this resource under Terraform management:
terraform import aws_instance.instance_i_0abc1234 i-0abc1234

# Then add the corresponding HCL resource block to your .tf files
```

### MODIFIED
```
# Revert live resource to its declared state:
terraform apply
#
# The following attributes will be changed:
#   instance_type: "t3.small" → "t2.micro"
#   monitoring: true → false
```

### TAG_DRIFT
```
# Resync tags to their declared values:
terraform apply
#
# The following tags will be updated:
#   env: "production" → "demo"
```

### Where it appears
- CLI table: truncated in Remediation column
- CLI JSON: full text in `remediation` field
- REST API: included in `/api/v1/scans/{id}/report`
- Dashboard: full text in drill-down panel with copy button ★

---

## 11. CLI

Built with **Typer**. Entry point: `driftctl`.

```
driftctl scan       Compare state against live AWS
driftctl report     View a saved scan from SQLite
driftctl scans list List recent scans
driftctl workspace create  Create a workspace
driftctl workspace list    List workspaces
driftctl schedule create   Set cron schedule
driftctl serve      Start REST API + dashboard
```

### scan options
```
--state PATH|s3://bucket/key   State source (required)
--config PATH                  YAML config file
--workspace TEXT               Workspace name from config
--region TEXT                  AWS region [default: us-east-1]
--profile TEXT                 AWS credential profile
--output table|json            [default: table]
--skip-cloud                   Parse state only, skip AWS calls
--unmanaged/--no-unmanaged     Detect unmanaged resources [default: off]
--verbose                      Include IN_SYNC in output
--output-file PATH             Write JSON to file
```

**Exit codes:** `0` no drift, `1` drift detected, `2` error

---

## 12. REST API

FastAPI on `:8080`. Auto-docs at `/docs`.

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check (always public) |
| GET | `/api/v1/workspaces` | List workspaces |
| POST | `/api/v1/workspaces` | Create workspace |
| GET | `/api/v1/workspaces/{id}` | Get workspace |
| DELETE | `/api/v1/workspaces/{id}` | Delete workspace |
| POST | `/api/v1/workspaces/{id}/scans` | Trigger scan (202 + background) |
| PUT | `/api/v1/workspaces/{id}/schedules` | Update cron |
| GET | `/api/v1/scans` | List scans |
| GET | `/api/v1/scans/{id}` | Get scan metadata |
| GET | `/api/v1/scans/{id}/report` | Full report (`?format=json\|table`) |
| GET | `/api/v1/scans/{id}/summary` | Lightweight summary (dashboard polling) |

Response envelope: `{"data": {...}, "error": null}`

---

## 13. Web Dashboard ★

Single-file `driftctl/api/static/index.html` — vanilla HTML + JS.
No build step. No npm. No webpack. Served by FastAPI at `GET /`.

### View 1 — Dashboard Home
- Aggregate stat cards: MISSING / UNMANAGED / MODIFIED / TAG DRIFT
- Workspace cards with last-scan drift count and **Scan Now** button
- Scan Now polls `/api/v1/scans/{id}/summary` every 3s until complete
- Recent scans table — every row clickable → scan detail
- Auto-refreshes every 30 seconds

### View 2 — Scan History
- All scans, newest first
- Workspace, state path, drifted count, total, status, timestamp
- Every row clickable → scan detail

### View 3 — Scan Detail ★
Per-resource drill-down (your original addition):
- Scan metadata: ID, state path, region, timestamp
- Summary counts per drift type
- Drift results table with expandable rows
- Click any row → inline panel shows:
  - **Attribute Changes table**: Field | Expected (green) | Actual (red)
  - **Tag Changes table**: Tag Key | Expected | Actual
  - **Remediation ★**: full command in monospace code block + copy button
- JSON download button

### View 4 — Trends ★
- SVG line chart, one line per workspace
- X-axis = scan date, Y-axis = drifted resource count
- 7 / 30 / 90 day window selector
- Auto-refreshes every 60 seconds
- Pure SVG — no charting library dependency

---

## 14. Cron Scheduler

`driftctl/scheduler/jobs.py` — APScheduler `BackgroundScheduler`.

- Runs in the FastAPI server process (background thread)
- On startup: reads all workspaces with `schedule_cron` from SQLite, registers jobs
- `register_job(workspace_id, name, cron_expr)` — parses 5-field cron, replaces existing
- `remove_job(workspace_id)` — called on workspace delete
- Cron format: `minute hour day month day_of_week` (UTC)
- Examples: `"0 6 * * *"` (daily 06:00), `"0 */6 * * *"` (every 6h)

---

## 15. SQLite Persistence

`driftctl/storage/db.py` — stdlib `sqlite3` only.

```sql
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY, name TEXT UNIQUE, provider TEXT,
    state_backend TEXT, state_path TEXT, state_region TEXT,
    region TEXT, detect_unmanaged INTEGER, schedule_cron TEXT,
    created_at TEXT, last_scan_id TEXT
);

CREATE TABLE scans (
    id TEXT PRIMARY KEY, workspace_id TEXT, created_at TEXT,
    state_path TEXT, region TEXT, status TEXT,
    drifted_count INTEGER, total_resources INTEGER,
    exit_code INTEGER, error_message TEXT, summary_json TEXT
);

CREATE TABLE drift_results (
    id TEXT PRIMARY KEY, scan_id TEXT,
    type TEXT, resource_id TEXT, resource_name TEXT, status TEXT,
    attribute_diffs TEXT,  -- JSON
    tag_diffs TEXT,        -- JSON
    remediation TEXT       -- ★ your addition
);
```

Key functions: `save_scan()`, `get_scan()`, `list_scans()`,
`save_workspace()`, `list_workspaces()`, `update_schedule()`

---

## 16. Configuration

`configs/driftctl.yaml`

```yaml
database: driftctl.db
api:
  addr: ":8080"
  api_key: ""
default_region: us-east-1
default_profile: ""
scan:
  detect_unmanaged: false
workspaces:
  - name: prod
    provider: aws
    state:
      backend: s3
      bucket: my-tf-state-bucket
      key: prod/terraform.tfstate
      region: us-east-1
    regions: [us-east-1]
    schedule:
      cron: "0 6 * * *"
  - name: local-dev
    provider: aws
    state:
      backend: local
      path: ./terraform.tfstate
    regions: [us-east-1]
```

**Environment variables:**
- `DRIFTCTL_DB` — database file path
- `DRIFTCTL_REGION` — default AWS region
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE` — standard boto3 chain
- `DEMO_STATE_BUCKET`, `DEMO_STATE_KEY` — Render deployment demo workspace

---

## 17. Project Layout

```
terraform-drift-detector/
├── README.md
├── SPEC.md
├── pyproject.toml
├── Dockerfile
├── render.yaml
├── .gitignore
├── configs/driftctl.yaml
├── testdata/sample.tfstate
├── driftctl/
│   ├── cli.py
│   ├── config.py
│   ├── models.py
│   ├── state/
│   │   ├── reader.py
│   │   └── extractor.py
│   ├── providers/
│   │   ├── base.py
│   │   ├── aws.py
│   │   └── registry.py
│   ├── engine/
│   │   ├── drift.py
│   │   └── remediate.py       ★
│   ├── report/
│   │   ├── json_renderer.py
│   │   └── table_renderer.py
│   ├── api/
│   │   ├── server.py
│   │   ├── middleware.py
│   │   ├── routes/
│   │   │   ├── health.py
│   │   │   ├── workspaces.py
│   │   │   └── scans.py
│   │   └── static/
│   │       └── index.html     ★
│   ├── scheduler/
│   │   └── jobs.py
│   └── storage/
│       └── db.py
└── tests/
    ├── conftest.py
    ├── test_state_reader.py
    ├── test_state_extractor.py
    ├── test_aws_provider.py
    ├── test_drift_engine.py
    ├── test_remediation.py    ★
    ├── test_storage.py
    ├── test_api.py
    └── test_smoke.py
```

---

## 18. Testing Strategy

**Goal: full suite runs offline (no real AWS), deterministically, under 90 seconds.**

| File | Tests | What it covers |
|---|---|---|
| `test_state_reader.py` | 16 | Local + S3 read, version check, mode filter, error cases |
| `test_state_extractor.py` | 30 | Per-type normalisation, bool coercion, SG rule sorting |
| `test_aws_provider.py` | 37 | boto3 fetch + normalise (moto), parity tests |
| `test_drift_engine.py` | 30 | All 5 statuses, mixed scenarios, edge cases |
| `test_remediation.py` | 33 | Exact command strings per status, enrich_results ★ |
| `test_storage.py` | 32 | SQLite CRUD, config loader, env var overrides |
| `test_api.py` | 40 | All endpoints, API key auth, dashboard serving ★ |
| `test_smoke.py` | 17 | End-to-end CLI via subprocess |
| **Total** | **235** | |

**Key test pattern — parity tests** (`test_aws_provider.py::TestParityStateVsCloud`):
Same resource through both extractors must produce identical output. If parity
fails, the drift engine produces false positives on every scan.

---

## 19. Packaging & Deployment

### Local
```bash
pip install -e ".[dev]"
driftctl scan --state terraform.tfstate --region us-east-1
driftctl serve  # http://localhost:8080
```

### Docker
```dockerfile
FROM python:3.11-slim
RUN useradd -m driftctl
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
RUN mkdir -p /data && chown driftctl:driftctl /data
USER driftctl
EXPOSE 8080
ENV DRIFTCTL_DB=/data/driftctl.db
ENTRYPOINT ["driftctl"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
```

### Render (live deployment)
- Runtime: Docker
- Plan: Free
- Environment variables:
  - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`
  - `DRIFTCTL_DB=/data/driftctl.db`
  - `DEMO_STATE_BUCKET`, `DEMO_STATE_KEY`
- On startup: auto-creates demo workspace from S3 tfstate, runs initial scan

---

## 20. Build Phases

| Phase | Deliverable | Tests |
|---|---|---|
| 1 | State reader (local + S3), extractor for all 5 types | 46 |
| 2 | AWS cloud fetcher, moto mocks, parity tests | +37 = 83 |
| 3 | Drift engine, remediation hints ★, JSON + table renderers | +63 = 146 |
| 4 | CLI (all commands), exit codes, smoke tests | +17 = 163 |
| 5 | SQLite persistence, YAML config loader | +32 = 195 |
| 6 | REST API (7 endpoints), APScheduler, API key auth | +35 = 230 |
| 7 | Basic web dashboard (home + scan history) | +5 = 235 |
| 8 | Enhanced dashboard (drill-down ★ + trends ★) | included in phase 7 |

Phases 1–7 = Abhishek's feature set in Python.
Phases 3 (remediation) and 8 (enhanced dashboard) = original additions.

---

## 21. Dependencies

### Runtime
| Package | Purpose |
|---|---|
| `boto3` | AWS API calls + S3 state backend |
| `typer` | CLI framework |
| `rich` | Terminal table + colour output |
| `pyyaml` | Config file parsing |
| `fastapi` | REST API |
| `uvicorn` | ASGI server |
| `apscheduler` | Cron scheduler |

### Development
| Package | Purpose |
|---|---|
| `pytest` | Test runner |
| `pytest-cov` | Coverage |
| `moto[ec2,s3]` | AWS mocking (offline tests) |
| `httpx` | FastAPI TestClient |
| `ruff` | Linter + formatter |
| `mypy` | Type checker |

### Standard library (no install)
`sqlite3`, `json`, `uuid`, `datetime`, `pathlib`, `abc`, `dataclasses`
