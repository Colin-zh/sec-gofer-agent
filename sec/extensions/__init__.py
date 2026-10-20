"""Public extension registration and lifecycle API."""

from sec.extensions.api import (
    ExtensionAPI,
    ExtensionCatalog,
    ProviderFactory,
    StorageFactory,
    UIHandler,
)
from sec.extensions.manager import ENTRY_POINT_GROUP, ExtensionLoadError, ExtensionManager
from sec._types.extensions import Extension

__all__ = [
    "ENTRY_POINT_GROUP",
    "Extension",
    "ExtensionAPI",
    "ExtensionCatalog",
    "ExtensionLoadError",
    "ExtensionManager",
    "ProviderFactory",
    "StorageFactory",
    "UIHandler",
]
