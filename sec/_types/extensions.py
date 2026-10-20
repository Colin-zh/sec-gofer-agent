"""Stable extension lifecycle contracts."""

from __future__ import annotations

from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from sec.extensions.api import ExtensionAPI


class Extension(Protocol):
    """An explicitly enabled, reusable extension package entry point.

    A Client may deactivate and reactivate the same instance as its enabled set
    changes. Implementations must release owned resources on deactivation and
    support a later activation with a fresh ExtensionAPI.
    """

    name: str

    async def activate(self, api: ExtensionAPI) -> None:
        """Register contributions for this host instance."""

    async def deactivate(self) -> None:
        """Release extension-owned resources."""
