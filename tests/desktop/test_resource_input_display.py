from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import asyncio

from sec import AsyncSecClient
from sec.apps.cli import create_chat_commands
from sec.apps.cli.resources import CliResourceManager
from sec.apps.desktop.runtime import SecRuntime
from test_async_sec_client import CloseableProvider


class ResourceInputDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_resource_keeps_short_display_and_persists_target_mapping(self) -> None:
        class PausedProvider(CloseableProvider):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def complete(self, messages, tools):
                self.started.set()
                await self.release.wait()
                return await super().complete(messages, tools)

        from sec.apps.cli.chat_commands import ChatCommandOutcome
        provider = PausedProvider()
        with patch("sec.apis.client.create_provider", return_value=provider):
            async with AsyncSecClient(
                "display-test", "", provider="openai", model="test", api_key="test",
                env_file=None, session_store_backend="in_memory",
            ) as client:
                runtime = SecRuntime(Path.cwd())
                runtime._client = client
                history = []
                runtime.session_history.connect(history.append)
                task = asyncio.create_task(runtime._run_prompt("Initial"))
                try:
                    await asyncio.wait_for(provider.started.wait(), 3)
                    await runtime._handle_command_outcome(ChatCommandOutcome(
                        run_prompt="Expanded resource body\n\nUser request:\nshort",
                        display_prompt="/skill:review short",
                    ))
                    provider.release.set()
                    await asyncio.wait_for(task, 3)
                    self.assertEqual(
                        [text for role, text in history[-1] if role == "user"],
                        ["Initial", "/skill:review short"],
                    )
                    self.assertIn("Expanded resource body", client.session.messages[2].text)
                    runtime._queued_resource_inputs.clear()
                    runtime._publish_session_history()
                    self.assertIn(("user", "/skill:review short"), history[-1])
                finally:
                    provider.release.set()
                    if not task.done():
                        runtime._cancel_run()
                        await asyncio.wait_for(task, 3)

    async def test_resource_input_display_persists_without_changing_model_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / ".sec" / "skills"
            skills.mkdir(parents=True)
            content = "Very long skill instructions\n" * 100
            path = skills / "review.md"
            path.write_text("---\ndescription: Review code\n---\n" + content, encoding="utf-8")
            manager = CliResourceManager(root, user_root=root / "user")
            manager.reload()
            provider = CloseableProvider()
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with AsyncSecClient(
                    "display-test", "", provider="openai", model="test", api_key="test",
                    env_file=None, session_store_backend="database",
                    database_url=f"sqlite+aiosqlite:///{root / 'sessions.db'}",
                ) as client:
                    runtime = SecRuntime(root)
                    runtime._client = client
                    runtime._resources = manager
                    runtime._commands = create_chat_commands(client, resource_manager=manager)
                    histories = []
                    runtime.session_history.connect(histories.append)
                    await runtime._handle_input("/skill:review check this")
                    self.assertIn(content.strip(), client.session.messages[0].text)
                    self.assertEqual(histories[-1][0], ("user", "/skill:review check this"))
                    session_id = client.session.id
                async with AsyncSecClient(
                    "display-test", "", provider="openai", model="test", api_key="test",
                    env_file=None, session_store_backend="database",
                    database_url=f"sqlite+aiosqlite:///{root / 'sessions.db'}",
                ) as reopened:
                    await reopened.switch_session(session_id)
                    runtime._client = reopened
                    runtime._resources = None
                    runtime._publish_session_history()
                    self.assertEqual(histories[-1][0], ("user", "/skill:review check this"))
            runtime._resources = manager
            expanded = manager.get("skill", "review").content + "\n\nUser request:\ncheck legacy"
            self.assertEqual(runtime._legacy_resource_display(expanded), "/skill:review check legacy")
            self.assertEqual(runtime._legacy_resource_display("ordinary message"), "ordinary message")
