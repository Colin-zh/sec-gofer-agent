"""Validate opt-in extension discovery, registration and lifecycle."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from sec.apps.cli import SlashCommand, main
from sec.apis import AsyncSecClient, SecClient
from sec.apps.extension_ui import UIInputRequest, UIValueResponse
from sec.config import ConfigError
from sec.extensions import ENTRY_POINT_GROUP, ExtensionManager
from sec.exceptions import SecUIHandlerError
from sec._types.policy import PolicyDecision
from sec.storage.session_store.in_memory import InMemorySessionStore
from sec.tools import tool


class _EntryPoint:
    def __init__(self, name: str, extension: object) -> None:
        self.name = name
        self._extension = extension
        self.loads = 0

    def load(self) -> object:
        self.loads += 1
        return self._extension


class _Extension:
    def __init__(self, name: str, activate=None) -> None:
        self.name = name
        self._activate = activate
        self.lifecycle: list[str] = []

    async def activate(self, api) -> None:
        self.lifecycle.append("activate")
        if self._activate is not None:
            self._activate(api)

    async def deactivate(self) -> None:
        self.lifecycle.append("deactivate")


def _leaf_exceptions(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [
            leaf
            for nested in error.exceptions
            for leaf in _leaf_exceptions(nested)
        ]
    return [error]


class ExtensionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_lists_entry_points_without_importing_them(self) -> None:
        point = _EntryPoint("sample", _Extension("sample"))
        manager = ExtensionManager()

        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[point],
        ) as discover:
            self.assertEqual(manager.discover(), ("sample",))
            discover.assert_called_once_with(group=ENTRY_POINT_GROUP)

        self.assertEqual(point.loads, 0)
        await manager.aclose()

    async def test_only_explicitly_enabled_entry_points_are_loaded(self) -> None:
        registered = []
        extension = _Extension(
            "sample",
            lambda api: api.register_tool(
                tool(lambda: "ok", name="sample-tool", description="Sample tool.")
            ),
        )
        selected = _EntryPoint("sample", extension)
        ignored = _EntryPoint("ignored", AssertionError("must not load"))

        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[selected, ignored],
        ) as discover:
            manager = ExtensionManager()
            await manager.load(("sample",))
            registered.extend(item.definition.name for item in manager.catalog.tools)
            await manager.aclose()

        discover.assert_called_once_with(group=ENTRY_POINT_GROUP)
        self.assertEqual(selected.loads, 1)
        self.assertEqual(ignored.loads, 0)
        self.assertEqual(registered, ["sample-tool"])
        self.assertEqual(extension.lifecycle, ["activate", "deactivate"])

    async def test_enabled_extensions_can_be_changed_and_reactivated(self) -> None:
        first = _Extension("first")
        second = _Extension("second")
        points = [_EntryPoint("first", first), _EntryPoint("second", second)]
        manager = ExtensionManager()
        with patch("sec.extensions.manager.entry_points", return_value=points):
            await manager.load(("first",))
            await manager.set_enabled(("first", "second"))
            self.assertEqual(manager.enabled, ("first", "second"))
            await manager.set_enabled(("second",))
            self.assertEqual(manager.enabled, ("second",))
            await manager.set_enabled(("first", "second"))

        self.assertEqual(first.lifecycle, ["activate", "deactivate", "activate"])
        self.assertEqual(second.lifecycle, ["activate"])
        await manager.aclose()

    async def test_failed_dynamic_enable_rolls_back_new_contributions(self) -> None:
        first = _Extension(
            "first",
            lambda api: api.register_tool(
                tool(lambda: "first", name="shared", description="First tool.")
            ),
        )
        second = _Extension(
            "second",
            lambda api: api.register_tool(
                tool(lambda: "second", name="shared", description="Second tool.")
            ),
        )
        manager = ExtensionManager()
        with (
            patch(
                "sec.extensions.manager.entry_points",
                return_value=[
                    _EntryPoint("first", first),
                    _EntryPoint("second", second),
                ],
            ),
            self.assertRaisesRegex(ValueError, "conflicts with extension"),
        ):
            await manager.load(("first",))
            await manager.set_enabled(("first", "second"))

        self.assertEqual(manager.enabled, ("first",))
        self.assertEqual(
            [registered.definition.name for registered in manager.catalog.tools],
            ["shared"],
        )
        self.assertEqual(second.lifecycle, ["activate", "deactivate"])
        await manager.aclose()

    async def test_empty_enablement_does_not_scan_installed_packages(self) -> None:
        with patch("sec.extensions.manager.entry_points") as discover:
            manager = ExtensionManager()
            await manager.load(())
        discover.assert_not_called()
        await manager.aclose()

    async def test_activation_failure_rolls_back_in_reverse_order(self) -> None:
        first = _Extension("first")

        def fail(_api) -> None:
            raise ValueError("activation failed")

        second = _Extension("second", fail)
        points = [_EntryPoint("first", first), _EntryPoint("second", second)]
        manager = ExtensionManager()
        with (
            patch("sec.extensions.manager.entry_points", return_value=points),
            self.assertRaisesRegex(ValueError, "activation failed"),
        ):
            await manager.load(("first", "second"))

        self.assertEqual(first.lifecycle, ["activate", "deactivate"])
        self.assertEqual(second.lifecycle, ["activate", "deactivate"])
        await manager.aclose()

    async def test_duplicate_contribution_names_are_rejected(self) -> None:
        def register_duplicate(api) -> None:
            api.register_provider("sample", lambda _config: object())
            api.register_provider("sample", lambda _config: object())

        extension = _Extension("duplicate", register_duplicate)
        manager = ExtensionManager()
        with (
            patch(
                "sec.extensions.manager.entry_points",
                return_value=[_EntryPoint("duplicate", extension)],
            ),
            self.assertRaisesRegex(ValueError, "conflicts with extension"),
        ):
            await manager.load(("duplicate",))

    async def test_catalog_accepts_all_supported_contribution_categories(self) -> None:
        class AllowPolicy:
            def decide(self, _context):
                return None

        class Store:
            async def load(self, _session_id):
                return None

            async def append(self, *_args):
                return 1

        def activate(api) -> None:
            api.register_tool(
                tool(lambda: "ok", name="tool-one", description="Tool.")
            )
            api.register_provider("provider-one", lambda _config: object())
            api.register_policy("policy-one", AllowPolicy())
            api.register_storage_backend("store-one", lambda _config, _db: Store())
            api.register_ui_handler(
                "ui-one",
                lambda request: {
                    "type": "extension_ui_response",
                    "id": request["id"],
                    "cancelled": True,
                },
            )
            api.register_command(SlashCommand("custom", "Custom command.", lambda _: 0))
            api.register_before_run_hook(lambda _context: None)
            api.register_after_run_hook(lambda _outcome: None)
            api.register_run_event_hook(lambda _event: None)

        extension = _Extension("all-contributions", activate)
        manager = ExtensionManager()
        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[_EntryPoint("all-contributions", extension)],
        ):
            await manager.load(("all-contributions",))

        self.assertEqual([item.definition.name for item in manager.catalog.tools], ["tool-one"])
        self.assertIn("provider-one", manager.catalog.providers)
        self.assertIn("policy-one", manager.catalog.policies)
        self.assertIn("store-one", manager.catalog.storage_backends)
        self.assertIn("ui-one", manager.catalog.ui_handlers)
        self.assertEqual([item.name for item in manager.catalog.commands], ["custom"])
        self.assertEqual(len(manager.catalog.before_run_hooks), 1)
        self.assertEqual(len(manager.catalog.after_run_hooks), 1)
        self.assertEqual(len(manager.catalog.run_event_hooks), 1)
        await manager.aclose()

    async def test_cli_loads_only_explicitly_enabled_extension_commands(self) -> None:
        extension = _Extension(
            "cli-extension",
            lambda api: api.register_command(
                SlashCommand("custom", "Custom command.", lambda _: 7)
            ),
        )
        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[_EntryPoint("cli-extension", extension)],
        ):
            self.assertEqual(
                await asyncio.to_thread(
                    main,
                    ("/custom",),
                    enabled_extensions=("cli-extension",),
                ),
                7,
            )
        self.assertEqual(extension.lifecycle, ["activate", "deactivate"])

    async def test_async_client_uses_extension_provider_store_policy_and_tools(self) -> None:
        class Provider:
            async def complete(self, _messages, _tools):
                raise AssertionError("provider should not be called in this test")

        class Policy:
            def decide(self, _context):
                return PolicyDecision.allow()

        store = InMemorySessionStore()

        def activate(api) -> None:
            api.register_tool(
                tool(lambda: "ok", name="plugin-tool", description="Plugin tool.")
            )
            api.register_provider("plugin-provider", lambda _config: Provider())
            api.register_policy("plugin-policy", Policy())
            api.register_storage_backend("plugin-store", lambda _config, _db: store)

        extension = _Extension("client-extension", activate)
        client = AsyncSecClient(
            "assistant",
            "Instructions.",
            provider="plugin-provider",
            model="plugin-model",
            api_key="secret",
            session_store_backend="plugin-store",
            policy_name="plugin-policy",
            enabled_extensions=("client-extension",),
            env_file=None,
        )
        with (
            patch(
                "sec.extensions.manager.entry_points",
                return_value=[_EntryPoint("client-extension", extension)],
            ),
        ):
            async with client:
                self.assertEqual(
                    tuple(tool.definition.name for tool in client.agent.tools),
                    ("plugin-tool",),
                )
                self.assertIn("plugin-provider", client.extensions.providers)
                self.assertIn("plugin-policy", client.extensions.policies)
                self.assertIn("plugin-store", client.extensions.storage_backends)

        self.assertEqual(extension.lifecycle, ["activate", "deactivate"])

    async def test_async_client_updates_extension_contributions_while_idle(self) -> None:
        extension = _Extension(
            "dynamic",
            lambda api: api.register_tool(
                tool(lambda: "ok", name="dynamic-tool", description="Dynamic tool.")
            ),
        )
        client = AsyncSecClient(
            "assistant",
            "Instructions.",
            provider="openai",
            model="test-model",
            api_key="test-key",
            env_file=None,
        )
        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[_EntryPoint("dynamic", extension)],
        ):
            async with client:
                self.assertEqual(client.discover_extensions(), ("dynamic",))
                self.assertEqual(client.enabled_extensions, ())

                await client.set_enabled_extensions(("dynamic",))
                self.assertEqual(client.enabled_extensions, ("dynamic",))
                self.assertEqual(
                    tuple(item.definition.name for item in client.agent.tools),
                    ("dynamic-tool",),
                )

                await client.set_enabled_extensions(())
                self.assertEqual(client.enabled_extensions, ())
                self.assertEqual(client.agent.tools, ())

        self.assertEqual(
            extension.lifecycle,
            ["activate", "deactivate"],
        )

    async def test_async_client_restores_extensions_when_runtime_rebuild_fails(self) -> None:
        class Provider:
            async def complete(self, _messages, _tools):
                raise AssertionError("provider should not be called in this test")

        extension = _Extension(
            "provider-extension",
            lambda api: api.register_provider(
                "plugin-provider",
                lambda _config: Provider(),
            ),
        )
        client = AsyncSecClient(
            "assistant",
            "Instructions.",
            provider="plugin-provider",
            model="plugin-model",
            api_key="test-key",
            enabled_extensions=("provider-extension",),
            env_file=None,
        )
        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[_EntryPoint("provider-extension", extension)],
        ):
            async with client:
                with self.assertRaisesRegex(
                    ConfigError,
                    "no enabled extension provides model provider",
                ):
                    await client.set_enabled_extensions(())
                self.assertEqual(
                    client.enabled_extensions,
                    ("provider-extension",),
                )
                self.assertIn("plugin-provider", client.extensions.providers)

        self.assertEqual(
            extension.lifecycle,
            ["activate", "deactivate", "activate", "deactivate"],
        )

    async def test_client_cannot_disable_extension_backing_its_session_store(self) -> None:
        extension = _Extension(
            "store-extension",
            lambda api: api.register_storage_backend(
                "plugin-store",
                lambda _config, _database: InMemorySessionStore(),
            ),
        )
        client = AsyncSecClient(
            "assistant",
            "Instructions.",
            provider="openai",
            model="test-model",
            api_key="test-key",
            session_store_backend="plugin-store",
            enabled_extensions=("store-extension",),
            env_file=None,
        )
        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[_EntryPoint("store-extension", extension)],
        ):
            async with client:
                with self.assertRaisesRegex(
                    ConfigError,
                    "SessionStore is in use",
                ):
                    await client.set_enabled_extensions(())
                self.assertEqual(
                    client.enabled_extensions,
                    ("store-extension",),
                )

    async def test_async_client_reports_all_resource_close_failures(self) -> None:
        class Provider:
            async def complete(self, _messages, _tools):
                raise AssertionError("provider should not be called in this test")

            async def aclose(self) -> None:
                raise RuntimeError("provider close failed")

        class FailingShutdownExtension(_Extension):
            async def deactivate(self) -> None:
                self.lifecycle.append("deactivate")
                raise ValueError("extension close failed")

        extension = FailingShutdownExtension(
            "failing-shutdown",
            lambda api: api.register_provider(
                "plugin-provider", lambda _config: Provider()
            ),
        )
        client = AsyncSecClient(
            "assistant",
            "Instructions.",
            provider="plugin-provider",
            model="plugin-model",
            api_key="secret",
            enabled_extensions=("failing-shutdown",),
            env_file=None,
        )
        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[_EntryPoint("failing-shutdown", extension)],
        ):
            await client.__aenter__()
            with self.assertRaises(BaseExceptionGroup) as caught:
                await client.aclose()

        messages = {str(error) for error in _leaf_exceptions(caught.exception)}
        self.assertIn("provider close failed", messages)
        self.assertIn("extension close failed", messages)
        self.assertEqual(extension.lifecycle, ["activate", "deactivate"])

    async def test_async_client_preserves_initialization_error_during_cleanup(self) -> None:
        class FailingShutdownExtension(_Extension):
            async def deactivate(self) -> None:
                self.lifecycle.append("deactivate")
                raise ValueError("extension cleanup failed")

        extension = FailingShutdownExtension("bad-cleanup")
        client = AsyncSecClient(
            "assistant",
            "Instructions.",
            enabled_extensions=("bad-cleanup",),
            env_file=None,
        )
        with (
            patch(
                "sec.extensions.manager.entry_points",
                return_value=[_EntryPoint("bad-cleanup", extension)],
            ),
            self.assertRaises(BaseExceptionGroup) as caught,
        ):
            await client.__aenter__()

        errors = _leaf_exceptions(caught.exception)
        self.assertTrue(any(isinstance(error, ConfigError) for error in errors))
        self.assertIn("extension cleanup failed", {str(error) for error in errors})
        self.assertEqual(extension.lifecycle, ["activate", "deactivate"])

    async def test_clients_dispatch_sync_and_async_ui_handlers_by_name(self) -> None:
        class Provider:
            async def complete(self, _messages, _tools):
                raise AssertionError("provider should not be called in this test")

        request: UIInputRequest = {
            "type": "extension_ui_request",
            "id": "request-1",
            "method": "input",
            "title": "Name",
        }

        async def handle_ui_request(
            ui_request: UIInputRequest,
        ) -> UIValueResponse:
            return {
                "type": "extension_ui_response",
                "id": ui_request["id"],
                "value": "Ada",
            }

        def handle_sync_ui_request(
            ui_request: UIInputRequest,
        ) -> UIValueResponse:
            return {
                "type": "extension_ui_response",
                "id": ui_request["id"],
                "value": "Ada",
            }

        def activate(api) -> None:
            api.register_provider("plugin-provider", lambda _config: Provider())
            api.register_ui_handler("prompt", handle_ui_request)
            api.register_ui_handler("sync-prompt", handle_sync_ui_request)

            def fail_ui_request(_request: UIInputRequest) -> UIValueResponse:
                raise ValueError("internal UI error detail")

            api.register_ui_handler("broken-prompt", fail_ui_request)

        async_extension = _Extension("async-ui", activate)
        async_client = AsyncSecClient(
            "assistant",
            "Instructions.",
            provider="plugin-provider",
            model="plugin-model",
            api_key="secret",
            enabled_extensions=("async-ui",),
            env_file=None,
        )
        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[_EntryPoint("async-ui", async_extension)],
        ):
            async with async_client:
                self.assertEqual(
                    await async_client.handle_ui_request("prompt", request),
                    {
                        "type": "extension_ui_response",
                        "id": "request-1",
                        "value": "Ada",
                    },
                )
                self.assertEqual(
                    await async_client.handle_ui_request("sync-prompt", request),
                    {
                        "type": "extension_ui_response",
                        "id": "request-1",
                        "value": "Ada",
                    },
                )
                with self.assertRaisesRegex(KeyError, "unknown"):
                    await async_client.handle_ui_request("unknown", request)
                with self.assertRaises(SecUIHandlerError) as caught:
                    await async_client.handle_ui_request("broken-prompt", request)
                self.assertEqual(caught.exception.handler_name, "broken-prompt")
                self.assertIsInstance(caught.exception.__cause__, ValueError)
                self.assertNotIn("internal UI error detail", str(caught.exception))

        sync_extension = _Extension("sync-ui", activate)
        sync_client = SecClient(
            "assistant",
            "Instructions.",
            provider="plugin-provider",
            model="plugin-model",
            api_key="secret",
            enabled_extensions=("sync-ui",),
            env_file=None,
        )

        def dispatch_from_sync_client() -> UIValueResponse:
            with sync_client:
                return sync_client.handle_ui_request("prompt", request)

        with patch(
            "sec.extensions.manager.entry_points",
            return_value=[_EntryPoint("sync-ui", sync_extension)],
        ):
            self.assertEqual(
                await asyncio.to_thread(dispatch_from_sync_client),
                {
                    "type": "extension_ui_response",
                    "id": "request-1",
                    "value": "Ada",
                },
            )
