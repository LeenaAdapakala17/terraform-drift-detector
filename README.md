# terraform-drift-detector

Detects configuration drift between Terraform state and live AWS infrastructure — without running `terraform plan` or `terraform apply`.

Built in Python on top of [Abhishek Veeramalla's terraform-drift-detector](https://github.com/iam-veeramalla/terraform-drift-detector), with two additions:

- **Remediation hints** — exact `terraform import` / `apply` / `state rm` command for every drift result
- **Enhanced dashboard** — scan history, per-resource drill-down with field diffs, drift trends chart

## What it detects

| Status | Meaning |
|---|---|
| `MISSING` | In Terraform state, deleted from AWS |
| `UNMANAGED` | In AWS, not in Terraform state |
| `MODIFIED` | Attributes changed out-of-band |
| `TAG_DRIFT` | Only tags changed |

## Supported resources

`aws_instance` · `aws_vpc` · `aws_subnet` · `aws_security_group` · `aws_s3_bucket`

## Quick start

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Project status

| Phase | Description | Status |
|---|---|---|
| 1 | State reader (local + S3) and extractor | ✅ Done |
| 2 | AWS cloud fetcher | 🔜 Next |
| 3 | Drift engine + remediation hints | 🔜 |
| 4 | CLI | 🔜 |
| 5 | SQLite persistence + config | 🔜 |
| 6 | REST API + scheduler | 🔜 |
| 7 | Basic dashboard | 🔜 |
| 8 | Enhanced dashboard | 🔜 |

## Credits

Foundation: [Abhishek Veeramalla — terraform-drift-detector](https://github.com/iam-veeramalla/terraform-drift-detector)