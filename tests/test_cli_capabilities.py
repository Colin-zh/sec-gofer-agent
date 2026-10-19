from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from sec.apps.cli.app import create_chat_commands
from sec.apps.cli.resources import CliResourceManager


class ChatCapabilityCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.resources = CliResourceManager(self.workspace)
        self.resources.reload()
        self.report = SimpleNamespace(
            generation=2,
            added=("agent/writer",),
            updated=(),
            removed=(),
            pending=("active Run keeps its snapshot",),
            issues=(),
        )
        self.cards = (
            SimpleNamespace(id="default", name="Default", description="General assistant"),
            SimpleNamespace(id="writer", name="Writer", description="Writes clearly"),
        )
        self.client = SimpleNamespace(
            session=SimpleNamespace(id="session"),
            extensions=SimpleNamespace(commands=()),
            enabled_extensions=(),
            discover_extensions=lambda: (),
            agent_cards=self.cards,
            selected_agent_id="default",
            pending_agent_id=None,
            worker_extensions=("safe-tools",),
            mcp_statuses=(
                {
                    "id": "docs",
                    "name": "Documentation server",
                    "status": "connected",
                    "enabled": True,
                },
            ),
            resources=self.resources,
            reload_resources=AsyncMock(return_value=self.report),
            select_agent=AsyncMock(),
            set_mcp_enabled=AsyncMock(return_value=self.report),
            set_worker_extensions=AsyncMock(return_value=self.report),
        )
        self.commands = create_chat_commands(self.client, resource_manager=self.resources)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_agent_commands_list_and_queue_selection_for_next_run(self) -> None:
        listing = await self.commands.dispatch("/agents")
        self.assertIn("default (selected)", listing.message)
        self.assertIn("writer — Writer: Writes clearly", listing.message)

        self.client.pending_agent_id = "writer"
        selection = await self.commands.dispatch("/agent writer")

        self.client.select_agent.assert_awaited_once_with("writer")
        self.assertIn("queued for the next Run", selection.message)

    async def test_reload_uses_shared_client_snapshot_during_active_run(self) -> None:
        run = SimpleNamespace(done=False)
        commands = create_chat_commands(
            self.client,
            get_active_run=lambda: run,
            resource_manager=self.resources,
        )

        outcome = await commands.dispatch("/reload")

        self.client.reload_resources.assert_awaited_once_with()
        self.assertIn("generation 2", outcome.message)
        self.assertIn("Pending: active Run keeps its snapshot", outcome.message)

    async def test_mcp_and_worker_plugin_commands_use_client_contract(self) -> None:
        listing = await self.commands.dispatch("/mcp list")
        self.assertIn("docs — Documentation server: connected (enabled)", listing.message)
        enabled = await self.commands.dispatch("/mcp disable docs")
        self.client.set_mcp_enabled.assert_awaited_once_with("docs", False)
        self.assertIn("MCP service docs disabled", enabled.message)

        plugins = await self.commands.dispatch("/plugins list")
        self.assertIn("safe-tools (enabled)", plugins.message)
        changed = await self.commands.dispatch("/plugins enable extra-tools")
        self.client.set_worker_extensions.assert_awaited_once_with(
            ("safe-tools", "extra-tools")
        )
        self.assertIn("extra-tools", changed.message)

    async def test_legacy_reload_extension_syntax_is_preserved(self) -> None:
        async def set_enabled(names: tuple[str, ...]) -> None:
            self.client.enabled_extensions = names

        self.client.set_enabled_extensions = AsyncMock(side_effect=set_enabled)
        result = await self.commands.dispatch("/reload enable legacy-plugin")
        self.client.set_enabled_extensions.assert_awaited_once_with(("legacy-plugin",))
        self.assertIn("Enabled legacy-plugin", result.message)


if __name__ == "__main__":
    unittest.main()
