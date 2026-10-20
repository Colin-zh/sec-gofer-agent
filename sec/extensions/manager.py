"""Explicitly load and manage installed sec extension entry points."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Iterable, Mapping

from sec._types.extensions import Extension
from sec._types.hooks import HOOK_NAMES
from sec.extensions.api import ExtensionAPI, ExtensionCatalog

ENTRY_POINT_GROUP = "sec.extensions"


class ExtensionLoadError(RuntimeError):
    """An enabled extension could not be discovered, activated, or closed."""


@dataclass(frozen=True)
class _ActiveExtension:
    """Keep each extension's contributions isolated for independent updates."""

    extension: Extension
    catalog: ExtensionCatalog


class ExtensionManager:
    """Discover installed extensions and update explicitly enabled contributions."""

    def __init__(self) -> None:
        self._catalog = ExtensionCatalog()
        self._active: dict[str, _ActiveExtension] = {}
        self._closed = False

    @property
    def catalog(self) -> ExtensionCatalog:
        """Return the merged contributions from enabled extensions."""
        return self._catalog

    @property
    def enabled(self) -> tuple[str, ...]:
        """Return explicitly enabled extensions in activation order."""
        return tuple(self._active)

    def discover(self) -> tuple[str, ...]:
        """List installed extension entry points without importing their code."""
        return tuple(sorted(self._entry_points()))

    async def load(self, enabled: Iterable[str]) -> None:
        """Discover installed entry points, importing only explicitly enabled ones."""
        if self._active or self._closed:
            raise RuntimeError("an ExtensionManager can only be loaded once")
        names = _validate_names(enabled)
        if not names:
            return

        by_name = self._entry_points()
        _require_available(names, by_name)
        try:
            for name in names:
                await self._enable_from_entry_point(name, by_name[name])
        except BaseException as error:
            cleanup_errors = await self._deactivate_active()
            self._closed = True
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "extension activation failed and rollback reported errors",
                    [error, *cleanup_errors],
                ) from error
            raise

    async def set_enabled(self, enabled: Iterable[str]) -> None:
        """Apply an explicit extension set, rolling back partial lifecycle changes."""
        self._ensure_open()
        names = _validate_names(enabled)
        by_name = self._entry_points()
        additions = tuple(name for name in names if name not in self._active)
        _require_available(additions, by_name)
        previous_order = self.enabled
        removals = tuple(name for name in previous_order if name not in names)
        added: list[str] = []
        removed: list[_ActiveExtension] = []

        try:
            for name in additions:
                await self._enable_from_entry_point(name, by_name[name])
                added.append(name)
            for name in removals:
                record = self._active[name]
                await self.disable(name)
                removed.append(record)
        except BaseException as error:
            cleanup_errors: list[BaseException] = []
            for name in reversed(added):
                try:
                    await self.disable(name)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            for name, record in zip(removals, removed, strict=False):
                try:
                    await self._activate_record(name, record)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            try:
                self._restore_order(previous_order)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "extension update failed and rollback reported errors",
                    [error, *cleanup_errors],
                ) from error
            raise

        self._restore_order(names)

    def preview_without(self, name: str) -> ExtensionCatalog:
        """Build the contribution catalog that would remain after disabling one."""
        if name not in self._active:
            raise KeyError(f"extension {name!r} is not enabled")
        remaining = {
            extension_name: record
            for extension_name, record in self._active.items()
            if extension_name != name
        }
        return _merge_catalogs(remaining)

    def owner_of(self, category: str, name: str) -> str | None:
        """Return the enabled extension that owns a named contribution."""
        return self._catalog.owner_of(category, name)

    async def aclose(self) -> None:
        """Deactivate loaded extensions in reverse order; report every failure."""
        if self._closed:
            return
        self._closed = True
        errors = await self._deactivate_active()
        if errors:
            raise ExceptionGroup("one or more extensions failed to deactivate", errors)

    async def disable(self, name: str) -> None:
        """Deactivate one extension and remove its contributions."""
        self._ensure_open()
        record = self._active.get(name)
        if record is None:
            raise KeyError(f"extension {name!r} is not enabled")
        remaining = {
            extension_name: active
            for extension_name, active in self._active.items()
            if extension_name != name
        }
        catalog = _merge_catalogs(remaining)
        await _deactivate(record.extension)
        self._active = remaining
        self._catalog = catalog

    async def _enable_from_entry_point(
        self,
        name: str,
        point: EntryPoint,
    ) -> None:
        if name in self._active:
            raise ValueError(f"extension {name!r} is already enabled")
        extension = point.load()
        if not _is_extension(extension, name):
            raise ExtensionLoadError(
                f"entry point {name!r} must resolve to an Extension instance "
                "with activate(api) and deactivate() methods"
            )
        record = await self._activate_record(
            name,
            _ActiveExtension(extension, ExtensionCatalog()),
            commit=False,
        )
        try:
            candidate = dict(self._active)
            candidate[name] = record
            catalog = _merge_catalogs(candidate)
        except BaseException as error:
            try:
                await _deactivate(extension)
            except Exception as cleanup_error:
                raise BaseExceptionGroup(
                    f"extension {name!r} activation failed and cleanup reported an error",
                    [error, cleanup_error],
                ) from error
            raise
        self._active = candidate
        self._catalog = catalog

    async def _activate_record(
        self,
        name: str,
        record: _ActiveExtension,
        *,
        commit: bool = True,
    ) -> _ActiveExtension:
        record = _ActiveExtension(record.extension, ExtensionCatalog())
        api = ExtensionAPI(name, record.catalog)
        try:
            result = record.extension.activate(api)
            if inspect.isawaitable(result):
                await result
            if commit:
                candidate = dict(self._active)
                candidate[name] = record
                catalog = _merge_catalogs(candidate)
                self._active = candidate
                self._catalog = catalog
        except BaseException as error:
            try:
                await _deactivate(record.extension)
            except Exception as cleanup_error:
                raise BaseExceptionGroup(
                    f"extension {name!r} activation failed and cleanup reported an error",
                    [error, cleanup_error],
                ) from error
            raise
        finally:
            api._deactivate()
        return record

    def _restore_order(self, names: Iterable[str]) -> None:
        ordered = {
            name: self._active[name]
            for name in names
            if name in self._active
        }
        ordered.update(
            (name, record)
            for name, record in self._active.items()
            if name not in ordered
        )
        self._active = ordered
        self._catalog = _merge_catalogs(ordered)

    def _entry_points(self) -> dict[str, EntryPoint]:
        by_name: dict[str, EntryPoint] = {}
        for point in entry_points(group=ENTRY_POINT_GROUP):
            if point.name in by_name:
                raise ExtensionLoadError(
                    f"duplicate {ENTRY_POINT_GROUP} entry point {point.name!r}"
                )
            by_name[point.name] = point
        return by_name

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ExtensionManager is closed")

    async def _deactivate_active(self) -> list[Exception]:
        errors: list[Exception] = []
        while self._active:
            name = next(reversed(self._active))
            record = self._active[name]
            try:
                await _deactivate(record.extension)
            except Exception as exc:
                errors.append(exc)
            self._active.pop(name)
        self._catalog = ExtensionCatalog()
        return errors


async def _deactivate(extension: Extension) -> None:
    result = extension.deactivate()
    if inspect.isawaitable(result):
        await result


def _validate_names(enabled: Iterable[str]) -> tuple[str, ...]:
    if isinstance(enabled, str):
        raise TypeError("enabled extensions must be an iterable of names, not text")
    names = tuple(enabled)
    if any(
        not isinstance(name, str)
        or re.fullmatch(r"[a-z][a-z0-9-]*", name) is None
        for name in names
    ):
        raise ValueError("extension names must be lowercase kebab-case")
    if len(names) != len(set(names)):
        raise ValueError("enabled extension names must be unique")
    return names


def _require_available(
    names: Iterable[str],
    available: Mapping[str, EntryPoint],
) -> None:
    missing = [name for name in names if name not in available]
    if missing:
        raise ExtensionLoadError(
            f"enabled extension entry point not found: {', '.join(missing)}"
        )


def _merge_catalogs(
    extensions: Mapping[str, _ActiveExtension],
) -> ExtensionCatalog:
    """Merge per-extension contributions while preserving conflict checks."""
    merged = ExtensionCatalog()
    categories = (
        ("tool", "tools", lambda value: value.definition.name),
        ("provider", "providers", lambda value: value),
        ("policy", "policies", lambda value: value),
        ("storage backend", "storage_backends", lambda value: value),
        ("UI handler", "ui_handlers", lambda value: value),
        ("command", "commands", lambda value: value.name),
    )
    for owner, record in extensions.items():
        source = record.catalog
        for category, attribute, name_of in categories:
            values = getattr(source, attribute)
            target = getattr(merged, f"_{attribute}")
            items = values.items() if isinstance(values, Mapping) else (
                (name_of(value), value) for value in values
            )
            for name, value in items:
                merged._claim(category, name, owner)
                if isinstance(target, dict):
                    target[name] = value
                else:
                    target.append(value)
        for hook_name in HOOK_NAMES:
            merged._hooks[hook_name].extend(source._hooks[hook_name])
    return merged


def _is_extension(value: object, name: str) -> bool:
    return (
        getattr(value, "name", None) == name
        and callable(getattr(value, "activate", None))
        and callable(getattr(value, "deactivate", None))
    )
