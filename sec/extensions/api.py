"""Typed registration API and contribution catalog for extensions."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from sec.apps.extension_ui import ExtensionUIRequest, ExtensionUIResponse
from sec._types.extensions import AfterRunHook, BeforeRunHook, RunEventHook
from sec._types.policy import PolicyEngine
from sec._types.providers import ModelProvider, StreamingModelProvider
from sec._types.storage import SessionStoreProtocol
from sec.config import Config
from sec.storage.resources.database import Database
from sec.tools.tool import Tool

if TYPE_CHECKING:
    from sec.apps.cli.slash_commands import SlashCommand


ProviderFactory = Callable[[Config], ModelProvider | StreamingModelProvider]
StorageFactory = Callable[[Config, Database | None], SessionStoreProtocol]
UIHandler = Callable[
    [ExtensionUIRequest],
    ExtensionUIResponse | Awaitable[ExtensionUIResponse],
]


class ExtensionCatalog:
    """Read-only view of contributions registered by enabled extensions."""

    def __init__(self) -> None:
        self._tools: list[Tool] = []
        self._providers: dict[str, ProviderFactory] = {}
        self._policies: dict[str, PolicyEngine] = {}
        self._storage_backends: dict[str, StorageFactory] = {}
        self._ui_handlers: dict[str, UIHandler] = {}
        self._commands: dict[str, SlashCommand] = {}
        self._before_run: list[BeforeRunHook] = []
        self._after_run: list[AfterRunHook] = []
        self._run_events: list[RunEventHook] = []
        self._owners: dict[tuple[str, str], str] = {}

    @property
    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools)

    @property
    def providers(self) -> Mapping[str, ProviderFactory]:
        return MappingProxyType(self._providers)

    @property
    def policies(self) -> Mapping[str, PolicyEngine]:
        return MappingProxyType(self._policies)

    @property
    def storage_backends(self) -> Mapping[str, StorageFactory]:
        return MappingProxyType(self._storage_backends)

    @property
    def ui_handlers(self) -> Mapping[str, UIHandler]:
        return MappingProxyType(self._ui_handlers)

    @property
    def commands(self) -> tuple[SlashCommand, ...]:
        return tuple(self._commands.values())

    @property
    def before_run_hooks(self) -> tuple[BeforeRunHook, ...]:
        return tuple(self._before_run)

    @property
    def after_run_hooks(self) -> tuple[AfterRunHook, ...]:
        return tuple(self._after_run)

    @property
    def run_event_hooks(self) -> tuple[RunEventHook, ...]:
        return tuple(self._run_events)

    def owner_of(self, category: str, name: str) -> str | None:
        """Return the extension that registered a named contribution."""
        return self._owners.get((category, name))

    def _claim(self, category: str, name: str, owner: str) -> None:
        key = (category, name)
        previous = self._owners.get(key)
        if previous is not None:
            raise ValueError(
                f"{category} contribution {name!r} from extension {owner!r} "
                f"conflicts with extension {previous!r}"
            )
        self._owners[key] = owner


class ExtensionAPI:
    """Capabilities an extension may register during activation."""

    def __init__(self, name: str, catalog: ExtensionCatalog) -> None:
        self.name = name
        self._catalog = catalog
        self._active = True

    def register_tool(self, registered: Tool) -> None:
        self._ensure_active()
        if not isinstance(registered, Tool):
            raise TypeError("extension tools must be Tool instances")
        tool_name = registered.definition.name
        self._catalog._claim("tool", tool_name, self.name)
        self._catalog._tools.append(registered)

    def register_provider(self, name: str, factory: ProviderFactory) -> None:
        if name in {"openai", "anthropic"}:
            raise ValueError(f"provider {name!r} is built in and cannot be overridden")
        self._register_named(
            "provider",
            name,
            factory,
            self._catalog._providers,
            "provider factory",
        )

    def register_policy(self, name: str, policy: PolicyEngine) -> None:
        self._register_named(
            "policy",
            name,
            policy,
            self._catalog._policies,
            "policy",
        )

    def register_storage_backend(self, name: str, factory: StorageFactory) -> None:
        self._register_named(
            "storage backend",
            name,
            factory,
            self._catalog._storage_backends,
            "storage factory",
        )

    def register_ui_handler(self, name: str, handler: UIHandler) -> None:
        self._register_named(
            "UI handler",
            name,
            handler,
            self._catalog._ui_handlers,
            "UI handler",
        )

    def register_command(self, command: Any) -> None:
        self._ensure_active()
        from sec.apps.cli.slash_commands import SlashCommand

        if not isinstance(command, SlashCommand):
            raise TypeError("extension commands must be SlashCommand instances")
        self._catalog._claim("command", command.name, self.name)
        self._catalog._commands[command.name] = command

    def register_before_run_hook(self, hook: BeforeRunHook) -> None:
        self._register_hook(hook, self._catalog._before_run, "before-run hook")

    def register_after_run_hook(self, hook: AfterRunHook) -> None:
        self._register_hook(hook, self._catalog._after_run, "after-run hook")

    def register_run_event_hook(self, hook: RunEventHook) -> None:
        if inspect.iscoroutinefunction(hook):
            raise TypeError("Run event hooks must be synchronous and non-blocking")
        self._register_hook(hook, self._catalog._run_events, "Run event hook")

    def _register_named(
        self,
        category: str,
        name: str,
        value: Any,
        target: dict[str, Any],
        description: str,
    ) -> None:
        self._ensure_active()
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9-]*", name) is None:
            raise ValueError(f"{category} name must be lowercase kebab-case")
        if not callable(value) and category not in ("policy",):
            raise TypeError(f"{description} must be callable")
        if category == "policy" and not callable(getattr(value, "decide", None)):
            raise TypeError("policy must implement decide()")
        self._catalog._claim(category, name, self.name)
        target[name] = value

    def _register_hook(self, hook: Callable[..., Any], target: list[Any], label: str) -> None:
        self._ensure_active()
        if not callable(hook):
            raise TypeError(f"{label} must be callable")
        target.append(hook)

    def _ensure_active(self) -> None:
        if not self._active:
            raise RuntimeError("extension registration is only allowed during activation")

    def _deactivate(self) -> None:
        self._active = False
