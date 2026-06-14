"""
driftctl/providers/registry.py

Maps provider name strings to CloudProvider instances.
Add new providers here as they are implemented.
"""

from __future__ import annotations

from driftctl.providers.aws import AWSProvider
from driftctl.providers.base import CloudProvider


def DefaultRegistry(
    region: str,
    profile: str | None = None,
) -> dict[str, CloudProvider]:
    """
    Return the default provider registry.

    Args:
        region:  AWS region
        profile: Optional AWS named credential profile

    Returns:
        Dict mapping provider name → CloudProvider instance.
        e.g. {"aws": AWSProvider(...)}
    """
    return {
        "aws": AWSProvider(region=region, profile=profile),
    }
