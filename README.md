# terraform-drift-detector

> Detects configuration drift between Terraform state and live AWS infrastructure — without running `terraform plan` or `terraform apply`.

**Built in Python** on top of [Abhishek Veeramalla's terraform-drift-detector](https://github.com/iam-veeramalla/terraform-drift-detector), with two original additions:
- **Remediation Hints** — exact `terraform import` / `apply` / `state rm` command for every drift result
- **Enhanced Dashboard** — scan history, per-resource drill-down with field diffs, drift trends chart

---

## 🔴 Live Demo

**[https://driftctl.onrender.com](https://driftctl.onrender.com)**

The live demo runs real drift detection against a real AWS account via an S3-backed Terraform state file. Click **Scan Now** to see live results.

---

## What it detects

| Status | Meaning | Example |
|---|---|---|
| `MISSING` | In Terraform state, deleted from AWS | EC2 instance removed from console |
| `UNMANAGED` | In AWS, not in Terraform state | Security group created outside Terraform |
| `MODIFIED` | Attributes changed out-of-band | Instance type resized manually |
| `TAG_DRIFT` | Only tags changed | `env` tag changed from `demo` to `production` |

## Supported AWS resources

| Terraform type | AWS API |
|---|---|
| `aws_instance` | `ec2:DescribeInstances` |
| `aws_vpc` | `ec2:DescribeVpcs` |
| `aws_subnet` | `ec2:DescribeSubnets` |
| `aws_security_group` | `ec2:DescribeSecurityGroups` |
| `aws_s3_bucket` | `s3:ListBuckets` + 5 per-bucket calls |

All API calls are read-only. The tool never creates, modifies, or deletes any resource.

---

## Built on top of Abhishek Veeramalla's terraform-drift-detector

This project ports Abhishek's Go-based terraform-drift-detector to Python and extends it with two original contributions.

### What Abhishek's original provides (fully implemented here in Python)
- Terraform state reading from **local file** and **S3 bucket**
- Live AWS resource fetching and normalisation for all 5 resource types
- Drift engine: MISSING, UNMANAGED, MODIFIED, TAG_DRIFT, IN_SYNC
- CLI with `scan`, `report`, `workspace`, `schedule` commands
- REST API with 7 endpoints
- SQLite persistence (scans, workspaces, schedules)
- YAML configuration with workspace definitions
- Cron-based scheduled scanning via APScheduler
- Basic web dashboard

### What this project adds on top ★
**1. Remediation Hints**
For every drift result, the tool emits the exact Terraform command to fix it:
- `UNMANAGED` → `terraform import aws_instance.web i-0abc1234`
- `MISSING` → `terraform apply` or `terraform state rm 'aws_instance.web'`
- `MODIFIED` → `terraform apply` with a field-by-field change summary
- `TAG_DRIFT` → `terraform apply` with tag-by-tag breakdown

**2. Enhanced Web Dashboard**
- **Scan history view** — every scan timestamped with drift counts
- **Per-resource drill-down** — click any drifted resource to see expected vs actual field diffs side by side
- **Copy-to-clipboard remediation** — copy the exact fix command with one click
- **Drift trends chart** — SVG line chart showing drift count over time per workspace

---

## Quick Start

### Install

```bash
git clone https://github.com/LeenaAdapakala17/terraform-drift-detector
cd terraform-drift-detector
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Run tests

```bash
pytest tests/ -v
# 235 passed
```

### Scan a local state file (offline mode)

```bash
driftctl scan --state testdata/sample.tfstate --skip-cloud --output table
```

### Scan against real AWS

```bash
aws configure  # set your credentials
driftctl scan --state terraform.tfstate --region us-east-1 --output table
```

### Start the web dashboard

```bash
driftctl serve
# Open http://localhost:8080
```

### Scan with S3 state backend

```bash
driftctl scan --state s3://my-bucket/prod/terraform.tfstate --region us-east-1
```

---

## CLI Reference

```bash
# Scan commands
driftctl scan --state <path|s3://...> --region us-east-1 --output table
driftctl scan --state <path> --skip-cloud          # offline/CI mode
driftctl scan --state <path> --output json         # machine-readable
driftctl scan --state <path> --unmanaged           # detect extra resources

# Report commands
driftctl report <scan-id> --output table
driftctl scans list --limit 20

# Workspace commands
driftctl workspace create --name prod --state s3://bucket/key --region us-east-1
driftctl workspace list

# Schedule commands
driftctl schedule create --workspace prod --cron "0 6 * * *"

# Server
driftctl serve --host 0.0.0.0 --port 8080
```

**Exit codes:** `0` = no drift, `1` = drift detected, `2` = error

---

## REST API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/api/v1/workspaces` | List workspaces |
| `POST` | `/api/v1/workspaces` | Create workspace |
| `POST` | `/api/v1/workspaces/{id}/scans` | Trigger scan |
| `GET` | `/api/v1/scans` | List recent scans |
| `GET` | `/api/v1/scans/{id}/report` | Get drift report (`?format=json\|table`) |
| `PUT` | `/api/v1/workspaces/{id}/schedules` | Set cron schedule |

Auto-generated API docs: `http://localhost:8080/docs`

Optional API key auth: set `api.api_key` in `configs/driftctl.yaml` and pass `X-API-Key` header.

---

## Configuration

`configs/driftctl.yaml`:

```yaml
database: driftctl.db

api:
  addr: ":8080"
  api_key: ""

default_region: us-east-1

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

---

## Architecture

```
Local .tfstate / S3 ──→ State Reader ──→ State Extractor ──→ Expected Model ──┐
                                                                               ├──→ Drift Engine ──→ Remediation ★ ──→ Report
AWS APIs (boto3)     ──→ Cloud Fetcher ──→ Cloud Extractor ──→ Actual Model  ──┘
                                                                               │
                                                                          SQLite DB
                                                                               │
                                                               ┌───────────────┼───────────────┐
                                                               │               │               │
                                                             CLI          REST API      Dashboard ★
```

---

## Project Structure

```
terraform-drift-detector/
├── driftctl/
│   ├── cli.py                    # Typer CLI
│   ├── config.py                 # YAML config loader
│   ├── models.py                 # Resource, DriftResult, ScanReport
│   ├── state/
│   │   ├── reader.py             # Local + S3 state reader
│   │   └── extractor.py          # tfstate → Resource normalisation
│   ├── providers/
│   │   ├── base.py               # CloudProvider ABC
│   │   ├── aws.py                # boto3 fetcher + normalisation
│   │   └── registry.py
│   ├── engine/
│   │   ├── drift.py              # Drift detection (pure function)
│   │   └── remediate.py          # Remediation hints ★
│   ├── report/
│   │   ├── json_renderer.py      # JSON output
│   │   └── table_renderer.py     # Rich terminal table
│   ├── api/
│   │   ├── server.py             # FastAPI app
│   │   ├── middleware.py         # API key auth
│   │   ├── routes/               # 7 REST endpoints
│   │   └── static/index.html     # Web dashboard ★
│   ├── scheduler/jobs.py         # APScheduler cron
│   └── storage/db.py             # SQLite persistence
├── tests/                        # 235 tests (moto mocked)
├── testdata/sample.tfstate        # Hand-crafted demo state
├── configs/driftctl.yaml          # Example config
├── Dockerfile
└── pyproject.toml
```

---

## IAM Policy (least privilege)

The tool only needs read-only AWS access:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeInstances",
      "ec2:DescribeVpcs",
      "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups",
      "s3:ListAllMyBuckets",
      "s3:GetBucketVersioning",
      "s3:GetBucketEncryption",
      "s3:GetBucketTagging",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketLogging",
      "s3:GetObject"
    ],
    "Resource": "*"
  }]
}
```

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| AWS SDK | boto3 |
| CLI | Typer + Rich |
| REST API | FastAPI + Uvicorn |
| Scheduler | APScheduler |
| Persistence | SQLite (stdlib) |
| Config | PyYAML |
| Testing | pytest + moto (offline AWS mocking) |
| Deployment | Docker + Render |

---

## Test Coverage

```
235 tests — all passing
├── test_state_reader.py      Local + S3 backend, error handling
├── test_state_extractor.py   Per-type normalisation for all 5 resources
├── test_aws_provider.py      boto3 fetch + parity tests (moto mocked)
├── test_drift_engine.py      All 5 drift statuses + edge cases
├── test_remediation.py       Exact command strings per status ★
├── test_storage.py           SQLite CRUD + config loader
├── test_api.py               All 7 REST endpoints + dashboard ★
└── test_smoke.py             End-to-end CLI tests
```

---

## Credits

Foundation: [Abhishek Veeramalla — terraform-drift-detector](https://github.com/iam-veeramalla/terraform-drift-detector)

This project ports Abhishek's Go implementation to Python and extends it with remediation hints and an enhanced dashboard.
