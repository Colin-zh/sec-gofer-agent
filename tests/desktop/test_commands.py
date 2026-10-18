from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sec.apps.cli import create_chat_commands
from sec.apps.desktop.runtime import SecRuntime


class _RunStub:
    def __init__(self) -> None:
        self.follow_ups: list[str] = []
        self.steering: list[str] = []
        self.cancelled = False
        self.done = False

    def follow_up(self, text: str) -> bool:
        self.follow_ups.append(text)
        return True

    def steer(self, text: str) -> bool:
        self.steering.append(text)
        return True

    def cancel(self) -> bool:
        self.cancelled = True
        return True


class DesktopCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_restores_empty_history_after_confirmation(self) -> None:
        from sec import AsyncSecClient
        from test_async_sec_client import CloseableProvider

        with patch("sec.apis.client.create_provider", return_value=CloseableProvider()):
            async with AsyncSecClient(
                "desktop-test", "", provider="openai", model="test", api_key="test",
                env_file=None, session_store_backend="in_memory",
            ) as client:
                runtime = SecRuntime(Path.cwd())
                runtime._client = client
                runtime._commands = create_chat_commands(
                    client, on_session_change=runtime._on_session_changed,
                )
                events = []
                runtime.command_result.connect(lambda message, kind: events.append(("notice", message)))
                runtime.session_history.connect(lambda history: events.append(("history", history)))
                previous_id = client.session.id
                with patch.object(runtime, "_refresh_sessions", new_callable=AsyncMock):
                    await runtime._handle_input("/new")
                self.assertNotEqual(client.session.id, previous_id)
                self.assertTrue(any(kind == "notice" for kind, _value in events))
                self.assertEqual(events[-1], ("history", ()))

    async def test_replace_send_waits_for_old_run_cleanup_before_starting_new_turn(self) -> None:
        from sec import AsyncSecClient
        from test_async_sec_client import CloseableProvider

        class PausedProvider(CloseableProvider):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.cancelled = False

            async def complete(self, messages, tools):
                if not self.started.is_set():
                    self.started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        self.cancelled = True
                        raise
                return await super().complete(messages, tools)

        provider = PausedProvider()
        with patch("sec.apis.client.create_provider", return_value=provider):
            async with AsyncSecClient(
                "desktop-test", "", provider="openai", model="test", api_key="test",
                env_file=None, session_store_backend="in_memory",
            ) as client:
                runtime = SecRuntime(Path.cwd())
                runtime._client = client
                runtime._commands = create_chat_commands(
                    client, get_active_run=lambda: runtime._active_run,
                )
                errors = []
                runtime.run_failed.connect(errors.append)
                runtime.command_result.connect(
                    lambda message, kind: errors.append(message) if kind == "error" else None
                )
                first = asyncio.create_task(runtime._submit_message("First", "replace"))
                try:
                    await asyncio.wait_for(provider.started.wait(), 3)
                    session_id = client.session.id
                    await asyncio.wait_for(runtime._submit_message("Replacement", "replace"), 3)
                    await asyncio.wait_for(first, 3)
                    self.assertTrue(provider.cancelled)
                    self.assertEqual(client.session.id, session_id)
                    self.assertEqual(
                        [m.text for m in client.session.messages if m.role == "user"],
                        ["First", "Replacement"],
                    )
                    self.assertEqual(errors, [])
                    self.assertIsNone(runtime._active_run)
                finally:
                    if not first.done():
                        runtime._cancel_run()
                        await asyncio.wait_for(first, 3)

    async def test_resource_inventory_uses_loaded_snapshot_and_enabled_extensions(self) -> None:
        from sec.apps.cli.resources import CliResourceManager

        runtime = SecRuntime(Path.cwd(), enabled_extensions=("hello",))
        runtime._resources = CliResourceManager(Path.cwd())
        runtime._client = SimpleNamespace(
            enabled_extensions=("updated",),
            discover_extensions=lambda: ("hello", "updated"),
        )
        results = []
        runtime.resources_updated.connect(results.append)
        runtime._publish_resources()
        self.assertEqual(len(results), 1)
        self.assertEqual(
            [(entry.name, entry.enabled) for entry in results[0].entries],
            [("hello", False), ("updated", True)],
        )

    async def test_rename_reports_failures_and_refreshes_only_after_success(self) -> None:
        runtime = SecRuntime(Path.cwd())
        rename = AsyncMock()
        runtime._client = SimpleNamespace(rename_session=rename)
        errors = []
        runtime.command_result.connect(lambda text, kind: errors.append((text, kind)))
        with patch.object(runtime, "_refresh_sessions", new_callable=AsyncMock) as refresh:
            await runtime._rename_session("other", "New name")
            rename.assert_awaited_once_with("other", "New name")
            refresh.assert_awaited_once()
            rename.side_effect = RuntimeError("active Run")
            await runtime._rename_session("other", "Failed")
            self.assertEqual(refresh.await_count, 1)
        self.assertEqual(errors[0][1], "error")
        self.assertIn("active Run", errors[0][0])

    async def test_help_and_extension_commands_are_dispatched_to_ui_output(self) -> None:
        def echo(args: tuple[str, ...]) -> int:
            print(" ".join(args))
            return 0

        client = SimpleNamespace(
            extensions=SimpleNamespace(
                commands=(
                    SimpleNamespace(
                        name="echo",
                        description="Echo text to the desktop.",
                        handler=echo,
                    ),
                )
            )
        )
        runtime = SecRuntime(Path.cwd())
        runtime._commands = create_chat_commands(client)
        results: list[tuple[str, str]] = []
        runtime.command_result.connect(
            lambda message, kind: results.append((message, kind))
        )

        await runtime._handle_input("/help")
        await runtime._handle_input("/echo hello desktop")

        self.assertIn("/new", results[0][0])
        self.assertIn("/resume", results[0][0])
        self.assertEqual(results[1], ("hello desktop", "info"))

    async def test_run_control_commands_target_the_active_sec_run(self) -> None:
        runtime = SecRuntime(Path.cwd())
        run = _RunStub()
        runtime._active_run = run  # type: ignore[assignment]
        runtime._commands = create_chat_commands(
            SimpleNamespace(extensions=SimpleNamespace(commands=())),
            get_active_run=lambda: runtime._active_run,
        )
        results: list[tuple[str, str]] = []
        runtime.command_result.connect(
            lambda message, kind: results.append((message, kind))
        )

        await runtime._handle_input("/followup continue after this")
        await runtime._handle_input("/steer focus on the result")
        await runtime._handle_input("/stop")

        self.assertEqual(run.follow_ups, ["continue after this"])
        self.assertEqual(run.steering, ["focus on the result"])
        self.assertTrue(run.cancelled)
        self.assertEqual(len(results), 3)

    async def test_steer_and_follow_alias_reach_real_runs_and_persist_messages(self) -> None:
        from sec import AsyncSecClient
        from test_async_sec_client import CloseableProvider

        class PausedProvider(CloseableProvider):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.cancelled = False

            async def complete(self, messages, tools):
                if not self.started.is_set():
                    self.started.set()
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        self.cancelled = True
                        raise
                return await super().complete(messages, tools)

        for command in ("steer", "follow", "followup"):
            with self.subTest(command=command):
                provider = PausedProvider()
                with patch("sec.apis.client.create_provider", return_value=provider):
                    async with AsyncSecClient(
                        "desktop-test", "", provider="openai", model="test", api_key="test",
                        env_file=None, session_store_backend="in_memory",
                    ) as client:
                        runtime = SecRuntime(Path.cwd())
                        runtime._client = client
                        runtime._commands = create_chat_commands(
                            client, get_active_run=lambda: runtime._active_run,
                        )
                        task = asyncio.create_task(runtime._run_prompt("Initial prompt"))
                        try:
                            await asyncio.wait_for(provider.started.wait(), 3)
                            text = 'keep "quotes", apostrophe\'s\nand a newline'
                            if command == "follow":
                                import shlex
                                await runtime._handle_input(f"/follow {shlex.quote(text)}")
                            else:
                                with patch.object(runtime, "submit_input") as submit:
                                    runtime.submit_control(command, text)
                                await runtime._handle_input(submit.call_args.args[0])
                            if command != "steer":
                                self.assertFalse(provider.cancelled)
                                provider.release.set()
                            await asyncio.wait_for(task, 3)
                            self.assertEqual(
                                [m.text for m in client.session.messages if m.role == "user"],
                                ["Initial prompt", text],
                            )
                            self.assertEqual(provider.cancelled, command == "steer")
                            if command != "steer":
                                self.assertEqual(provider.response_count, 2)
                        finally:
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)

    async def test_controls_without_active_run_report_requirement(self) -> None:
        runtime = SecRuntime(Path.cwd())
        runtime._commands = create_chat_commands(
            SimpleNamespace(extensions=SimpleNamespace(commands=())),
            get_active_run=lambda: None,
        )
        results = []
        runtime.command_result.connect(lambda message, kind: results.append(message))
        for command in ("steer", "follow", "followup"):
            await runtime._handle_input(f"/{command} hello")
            self.assertIn("active Run", results[-1])

    async def test_session_actions_use_clicked_session_without_switching_before_export(self) -> None:
        from tempfile import TemporaryDirectory
        from sec import AsyncSecClient
        from sec.apps.cli.session_io import read_session_file
        from test_async_sec_client import CloseableProvider

        with patch("sec.apis.client.create_provider", return_value=CloseableProvider()):
            async with AsyncSecClient(
                "desktop-test", "", provider="openai", model="test", api_key="test",
                env_file=None, session_store_backend="in_memory",
            ) as client:
                runtime = SecRuntime(Path.cwd())
                runtime._client = client
                await client.run("Source history").result()
                source = client.session.id
                await client.switch_session()
                current = client.session
                results = []
                runtime.command_result.connect(lambda text, kind: results.append((text, kind)))
                with TemporaryDirectory() as temporary:
                    path = Path(temporary) / "session.json"
                    await runtime._session_action("export", source, str(path))
                    self.assertEqual(read_session_file(path).session_id, source)
                    self.assertIs(client.session, current)
                    original = path.read_bytes()
                    await runtime._session_action("export", source, str(path))
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(results[-1][1], "error")
                await runtime._session_action("fork", source)
                self.assertNotEqual(client.session.id, source)
                self.assertEqual(client.session.messages[0].text, "Source history")
                await runtime._session_action("clone", source)
                clone = client.session
                await runtime._session_action("delete", source)
                self.assertIs(client.session, clone)
                await runtime._session_action("delete", clone.id)
                self.assertNotEqual(client.session.id, clone.id)
                await runtime._session_action("open", "missing")
                self.assertEqual(results[-1][1], "error")


if __name__ == "__main__":
    unittest.main()
