"""Backward-compatible imports for the core Markdown resource catalog."""

from sec.resources.markdown import (
    MAX_RESOURCE_BYTES,
    CliResource,
    CliResourceManager,
    Issue,
    Resource,
    ResourceIssue,
    ResourceManager,
    ResourceSnapshot,
    Snapshot,
    json,
)

__all__ = [
    "MAX_RESOURCE_BYTES", "CliResource", "CliResourceManager", "Resource",
    "ResourceManager", "ResourceIssue", "ResourceSnapshot", "Issue", "Snapshot",
]
