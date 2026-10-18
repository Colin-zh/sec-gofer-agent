from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sec import PermissionModePolicy
from sec.apps.cli import create_chat_commands
from sec.apps.cli.resources import CliResourceManager
from sec.apps.desktop.runtime import SecRuntime
from sec.apps.desktop.settings import ExtensionSettings


class ToolConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        prompts = self.root / ".sec" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "daily.md").write_text("Daily prompt content", encoding="utf-8")
        self.runtime = SecRuntime(self.root)
        self.manager = CliResourceManager(
            self.root, user_root=self.root / "user",
            activation_path=self.root / ".sec" / "desktop-resources.json",
        )
        self.manager.reload()
        self.client = SimpleNamespace(
            session=SimpleNamespace(id="session", messages=()),
            extensions=SimpleNamespace(commands=()),
            enabled_extensions=(),
            discover_extensions=lambda: ("hello",),
        )

        async def set_enabled(names: tuple[str, ...]) -> None:
            if "broken" in names:
                raise RuntimeError("activation failed")
            self.client.enabled_extensions = names

        self.client.set_enabled_extensions = set_enabled
        self.runtime._client = self.client
        self.runtime._resources = self.manager
        self.runtime._policy = PermissionModePolicy("normal", workspace_root=self.root)
        self.runtime._extension_settings = ExtensionSettings(
            self.root / ".sec" / "desktop-extensions.json",
        )
        self.runtime._commands = create_chat_commands(
            self.client, resource_manager=self.manager,
            permission_policy=self.runtime._policy,
            get_active_run=lambda: self.runtime._active_run,
            refresh_completions=self.runtime.command_completions.emit,
        )
        self.results = []
        self.inventories = []
        self.completions = []
        self.runtime.configuration_result.connect(lambda *args: self.results.append(args))
        self.runtime.resources_updated.connect(self.inventories.append)
        self.runtime.command_completions.connect(self.completions.append)

    async def test_prompt_activation_updates_real_dispatch_completion_and_persistence(self) -> None:
        await self.runtime._configure_resource("prompt", "daily", False)
        self.assertEqual(self.results[-1][1], "info")
        self.assertIsNone(self.manager.get("prompt", "daily"))
        self.assertNotIn("/prompt:daily", dict(self.completions[-1]))
        self.assertFalse(self.inventories[-1].entries[0].enabled)
        self.assertIn("Daily prompt content", self.inventories[-1].entries[0].content)
        disabled = await self.runtime._commands.dispatch("/prompt:daily")
        self.assertIsNone(disabled.run_prompt)
        await self.runtime._configure_resource("prompt", "daily", True)
        restored = await self.runtime._commands.dispatch("/prompt:daily")
        self.assertIn("Daily prompt content", restored.run_prompt)
        self.assertIn("/prompt:daily", dict(self.completions[-1]))
        fresh = CliResourceManager(
            self.root, user_root=self.root / "user",
            activation_path=self.root / ".sec" / "desktop-resources.json",
        )
        fresh.reload()
        self.assertIsNotNone(fresh.get("prompt", "daily"))

    async def test_extension_live_activation_persistence_and_save_failure_rollback(self) -> None:
        await self.runtime._configure_resource("extension", "hello", True)
        self.assertEqual(self.client.enabled_extensions, ("hello",))
        settings = ExtensionSettings(self.runtime._extension_settings.path)
        self.assertEqual(settings.resolve(()), ("hello",))
        await self.runtime._configure_resource("extension", "broken", True)
        self.assertEqual(self.results[-1][1], "error")
        self.assertEqual(self.client.enabled_extensions, ("hello",))
        with patch.object(ExtensionSettings, "save", side_effect=OSError("disk full")):
            await self.runtime._configure_resource("extension", "hello", False)
        self.assertIn("disk full", self.results[-1][0])
        self.assertEqual(self.client.enabled_extensions, ("hello",))
        extension = next(e for e in self.inventories[-1].entries if e.kind == "extension")
        self.assertTrue(extension.enabled)
        await self.runtime._configure_resource("extension", "hello", False)
        self.assertEqual(self.client.enabled_extensions, ())
        self.assertEqual(
            ExtensionSettings(self.runtime._extension_settings.path).resolve(("hello",)), (),
        )

    async def test_busy_changes_and_mcp_are_explicitly_rejected(self) -> None:
        self.runtime._active_run = SimpleNamespace(done=False)
        await self.runtime._configure_resource("prompt", "daily", False)
        await self.runtime._configure_resource("extension", "hello", True)
        await self.runtime._configure_permission("auto")
        self.assertTrue(all(kind == "error" for _, kind in self.results))
        self.assertIsNotNone(self.manager.get("prompt", "daily"))
        self.assertEqual(self.runtime._policy.mode_name, "normal")
        self.runtime._active_run = None
        await self.runtime._configure_resource("mcp", "server", True)
        self.assertIn("尚未接入", self.results[-1][0])
        await self.runtime._configure_permission("plan")
        self.assertEqual(self.runtime._policy.mode_name, "plan")
        self.assertEqual(self.inventories[-1].permission_mode, "plan")
        await self.runtime._handle_input("/mode normal")
        self.assertEqual(self.inventories[-1].permission_mode, "normal")

    def test_extension_settings_reject_invalid_config_and_preserve_state_on_failed_save(self) -> None:
        path = self.root / "settings.json"
        path.write_text('{"hello": "yes"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            ExtensionSettings(path)
        path.unlink()
        settings = ExtensionSettings(path)
        settings.save("hello", True)
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                settings.save("hello", False)
        self.assertEqual(settings.resolve(()), ("hello",))
        self.assertEqual(ExtensionSettings(path).resolve(()), ("hello",))
