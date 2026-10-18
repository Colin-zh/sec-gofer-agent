"""Explicitly load and manage installed sec extension entry points."""

from __future__ import annotations

import inspect
import re
from importlib.metadata import EntryPoint, entry_points
from typing import Iterable

from sec._types.extensions import Extension
from sec.extensions.api import ExtensionAPI, ExtensionCatalog

ENTRY_POINT_GROUP = "sec.extensions"


class ExtensionLoadError(RuntimeError):
    """An enabled extension could not be discovered, activated, or closed."""


class ExtensionManager:
    """Load only selected extensions and close them in reverse activation order."""

    def __init__(self) -> None:
        self.catalog = ExtensionCatalog()
        self._active: list[Extension] = []
        self._closed = False

    async def load(self, enabled: Iterable[str]) -> None:
        """Discover installed entry points, importing only explicitly enabled ones."""
        if self._active or self._closed:
            raise RuntimeError("an ExtensionManager can only be loaded once")
        names = tuple(enabled)
        if any(
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9-]*", name) is None
            for name in names
        ):
            raise ValueError("extension names must be lowercase kebab-case")
        if len(names) != len(set(names)):
            raise ValueError("enabled extension names must be unique")
        if not names:
            return

        candidates = entry_points(group=ENTRY_POINT_GROUP)
        by_name: dict[str, EntryPoint] = {}
        for point in candidates:
            if point.name in by_name:
                raise ExtensionLoadError(
                    f"duplicate {ENTRY_POINT_GROUP} entry point {point.name!r}"
                )
            by_name[point.name] = point
        missing = [name for name in names if name not in by_name]
        if missing:
            raise ExtensionLoadError(
                f"enabled extension entry point not found: {', '.join(missing)}"
            )

        try:
            for name in names:
                extension = by_name[name].load()
                if not _is_extension(extension, name):
                    raise ExtensionLoadError(
                        f"entry point {name!r} must resolve to an Extension instance "
                        "with async activate(api) and deactivate() methods"
                    )
                self._active.append(extension)
                api = ExtensionAPI(name, self.catalog)
                try:
                    result = extension.activate(api)
                    if inspect.isawaitable(result):
                        await result
                finally:
                    api._deactivate()
        except BaseException as error:
            cleanup_errors = await self._deactivate_active()
            self._closed = True
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "extension activation failed and rollback reported errors",
                    [error, *cleanup_errors],
                ) from error
            raise

    async def aclose(self) -> None:
        """Deactivate loaded extensions in reverse order; report every failure."""
        if self._closed:
            return
        self._closed = True
        errors = await self._deactivate_active()
        if errors:
            raise ExceptionGroup("one or more extensions failed to deactivate", errors)

    async def _deactivate_active(self) -> list[Exception]:
        errors: list[Exception] = []
        while self._active:
            extension = self._active.pop()
            try:
                result = extension.deactivate()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(exc)
        return errors


def _is_extension(value: object, name: str) -> bool:
    return (
        getattr(value, "name", None) == name
        and callable(getattr(value, "activate", None))
        and callable(getattr(value, "deactivate", None))
    )
