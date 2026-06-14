"""
driftctl/providers/base.py

Abstract base class for cloud providers.
The AWS implementation is in aws.py.
Any future provider (GCP, Azure) implements this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from driftctl.models import Resource


class CloudProvider(ABC):

    @abstractmethod
    def fetch(self, resource_type: str) -> list[Resource]:
        """
        Fetch all live resources of the given type from the cloud.

        Handles pagination internally. Returns normalised Resource objects
        with source="actual". Unknown or unsupported types return [].

        Args:
            resource_type: e.g. "aws_instance", "aws_s3_bucket"

        Returns:
            List of normalised Resource objects.
        """

    @abstractmethod
    def supported_types(self) -> list[str]:
        """
        Return the list of resource types this provider can fetch.
        Only these types will be passed to fetch().
        """
