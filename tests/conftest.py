"""
tests/conftest.py

Shared pytest configuration and fixtures.

CRITICAL: moto requires AWS credentials to be set before it can mock
the AWS APIs. Without them, boto3 raises NoCredentialsError before
moto even gets a chance to intercept the call.

We set dummy values here via environment variables. These are not
real credentials — moto intercepts all API calls and never sends
anything to AWS. The values just need to be present.
"""

import os
import pytest


# ---------------------------------------------------------------------------
# Set dummy AWS credentials for moto BEFORE any test imports boto3.
# These are set at module load time so they are available for all tests.
# ---------------------------------------------------------------------------

os.environ.setdefault("AWS_ACCESS_KEY_ID",     "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN",    "testing")
os.environ.setdefault("AWS_SESSION_TOKEN",     "testing")
os.environ.setdefault("AWS_DEFAULT_REGION",    "us-east-1")
