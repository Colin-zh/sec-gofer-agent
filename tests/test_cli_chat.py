"""回归覆盖原生聊天 CLI 的命令、事件呈现和审批行为。"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from rich.console import Console

from sec._types.events import (
    AgentSettledEvent,
    ContentDeltaEvent,
    MessageUpdateEvent,
    ToolApprovalRequestedEvent,
)
from sec.apps.cli import create_chat_commands
from sec.apps.cli.app import (
    FAREWELL_MESSAGE,
    _chat_interactive,
    _chat_noninteractive,
    _create_commands,
)
from sec.apps.cli.chat_commands import (
    ChatCommand,
    ChatCommandOutcome,
    ChatCommandRegistry,
    create_default_chat_commands,
)
from sec.apps.cli.presentation import show_event, show_noninteractive_event
from sec.apps.cli.slash_commands import SlashCommand, SlashCommandRegistry
from sec.apps.cli.terminal import TerminalUI


class _FakeRun:
    def __init__(self) -> None:
        self.approved: list[str] = []
        self.denied: list[tuple[str, str | None]] = []

    def approve_tool(self, tool_call_id: str) -> bool:
        self.approved.append(tool_call_id)
        return True

    def deny_tool(self, tool_call_id: str, reason: str | None = None) -> bool:
        self.denied.append((tool_call_id, reason))
        return True


class ChatCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_and_argv_registries_share_catalog_metadata(self) -> None:
        chat_commands = ChatCommandRegistry()
        cli_commands = SlashCommandRegistry()
        chat_commands.register(
            ChatCommand(
                "inspect",
                "Inspect the current workspace",
                lambda _args: ChatCommandOutcome("done"),
            )
        )
        cli_commands.register(
            SlashCommand(
                "inspect",
                "Inspect the current workspace",
                lambda _args: 0,
            )
        )

        self.assertEqual(chat_commands.help_text(), cli_commands.help_text())
        self.assertEqual(
            chat_commands.completion_items()[0],
            cli_commands.completion_items()[0],
        )

    async def test_builtin_commands_route_control_and_support_aliases(self) -> None:
        follow_ups: list[str] = []
        steering: list[str] = []
        stopped: list[bool] = []
        commands = create_default_chat_commands(
            enqueue_follow_up=lambda text: follow_ups.append(text) or "queued",
            enqueue_steering=lambda text: steering.append(text) or "steered",
            request_stop=lambda: stopped.append(True) or "cancelled",
        )

        follow_up = await commands.dispatch('/follow-up "second message"')
        steer = await commands.dispatch("/steer change direction")
        stop = await commands.dispatch("/stop")
        help_outcome = await commands.dispatch("/help")

        self.assertEqual(follow_ups, ["second message"])
        self.assertEqual(steering, ["change direction"])
        self.assertEqual(stopped, [True])
        self.assertEqual(follow_up.message, "queued")
        self.assertEqual(steer.message, "steered")
        self.assertEqual(stop.message, "cancelled")
        self.assertIn("/followup", help_outcome.message)
        self.assertIsNone(await commands.dispatch("ordinary prompt"))

    async def test_trust_command_is_session_scoped_and_preserves_remote_approval(self) -> None:
        mode = ["normal"]
        commands = create_default_chat_commands(
            get_permission_mode=lambda: mode[0],
            change_permission_mode=lambda value: mode.__setitem__(0, value)
            or f"Tool permission mode set to {value}.",
        )

        enabled = await commands.dispatch("/trust on")
        status = await commands.dispatch("/trust")
        disabled = await commands.dispatch("/trust off")

        self.assertEqual(mode[0], "normal")
        self.assertIn("Bash and remote capabilities still require approval", enabled.message)
        self.assertIn("enabled for this chat only", status.message)
        self.assertIn("disabled for this chat only", disabled.message)

    async def test_extension_commands_are_available_in_chat(self) -> None:
        def echo(arguments: tuple[str, ...]) -> int:
            print(" ".join(arguments))
            return 0

        client = SimpleNamespace(
            extensions=SimpleNamespace(
                commands=(
                    SimpleNamespace(
                        name="echo",
                        description="Echo its arguments",
                        handler=echo,
                    ),
                )
            )
        )
        commands = create_chat_commands(client)

        outcome = await commands.dispatch("/echo hello world")

        self.assertEqual(outcome.message, "hello world")
        self.assertEqual(outcome.display_kind, "info")


class TerminalEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_delta_is_streamed_to_terminal(self) -> None:
        output = StringIO()
        ui = TerminalUI(output=output, force_terminal=False)
        event = MessageUpdateEvent(
            assistant_message_event=ContentDeltaEvent(
                type="text_delta",
                content_index=0,
                delta="hello",
            )
        )

        await show_event(ui, _FakeRun(), event, trace_id=1)

        self.assertIn("hello", output.getvalue())

    async def test_tool_approval_uses_run_control(self) -> None:
        output = StringIO()
        ui = TerminalUI(output=output, force_terminal=False)
        run = _FakeRun()
        event = ToolApprovalRequestedEvent(
            tool_call_id="call-1",
            tool_name="publish",
            args={"path": "release.txt"},
        )
        with patch.object(ui, "confirm", new=AsyncMock(return_value=True)):
            await show_event(ui, run, event, trace_id=1)

        self.assertEqual(run.approved, ["call-1"])
        self.assertEqual(run.denied, [])

    async def test_noninteractive_mode_denies_tool_approval(self) -> None:
        output = StringIO()
        console = Console(file=output, no_color=True, force_terminal=False)
        run = _FakeRun()
        event = ToolApprovalRequestedEvent(
            tool_call_id="call-2",
            tool_name="publish",
            args={"path": "release.txt"},
        )

        show_noninteractive_event(console, run, event)

        self.assertEqual(
            run.denied,
            [("call-2", "Tool approval requires an interactive terminal.")],
        )
        self.assertIn("approval requires an interactive terminal", output.getvalue())

    async def test_slash_command_during_approval_denies_and_dispatches(self) -> None:
        ui = TerminalUI(output=StringIO(), force_terminal=False)
        confirmation = asyncio.create_task(ui.confirm("Allow tool?", "details"))
        await asyncio.sleep(0)

        consumed = ui.resolve_confirmation("/stop")

        self.assertFalse(consumed)
        self.assertFalse(await confirmation)


class ChatLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_interactive_chat_starts_client_run_and_exits_cleanly(self) -> None:
        class FakeUI:
            def __init__(self, **_kwargs) -> None:
                self.input_queue: asyncio.Queue[str] = asyncio.Queue()
                self.input_queue.put_nowait("hello")
                self.input_queue.put_nowait("/exit")
                self.stopped = asyncio.Event()
                self.output: list[str] = []

            async def run(self) -> None:
                await self.stopped.wait()

            def add_rich(self, renderable) -> None:
                self.output.append(str(renderable))

            def set_command_completions(self, _items) -> None:
                pass

            def set_working(self, _working: bool) -> None:
                pass

            def add_status(self, _kind: str, text: str) -> None:
                self.output.append(text)

            def add_user_message(self, text: str) -> None:
                self.output.append(text)

            def resolve_confirmation(self, _answer: str) -> bool:
                return False

            def append_stream_text(self, text: str) -> None:
                self.output.append(text)

            def finish_turn(self) -> None:
                pass

            def stop(self) -> None:
                self.stopped.set()

            async def confirm(self, _title: str, _message: str) -> bool:
                return False

        class FakeRun(_FakeRun):
            done = False

            async def events(self):
                yield MessageUpdateEvent(
                    assistant_message_event=ContentDeltaEvent(
                        type="text_delta",
                        content_index=0,
                        delta="assistant response",
                    )
                )
                yield AgentSettledEvent(status="completed")

            async def result(self):
                self.done = True
                return SimpleNamespace(model_calls=1, tool_calls=0)

            def cancel(self) -> bool:
                self.done = True
                return True

            def follow_up(self, _text: str) -> bool:
                return True

            def steer(self, _text: str) -> bool:
                return True

        class FakeClient:
            session = SimpleNamespace(id="session-1")
            extensions = SimpleNamespace(commands=())

            def __init__(self) -> None:
                self.prompts: list[str] = []
                self.run_handle = FakeRun()

            def run(self, prompt: str) -> FakeRun:
                self.prompts.append(prompt)
                return self.run_handle

        client = FakeClient()
        ui_instances: list[FakeUI] = []

        def create_ui(**kwargs) -> FakeUI:
            ui = FakeUI(**kwargs)
            ui_instances.append(ui)
            return ui

        with (
            patch("sec.apps.cli.app.TerminalUI", side_effect=create_ui),
            patch("sec.apps.cli.app.welcome_panel", return_value="welcome"),
        ):
            await _chat_interactive(client)

        self.assertEqual(client.prompts, ["hello"])
        self.assertIn("assistant response", ui_instances[0].output)
        self.assertIn(
            f"{FAREWELL_MESSAGE}\nSession ID: session-1",
            ui_instances[0].output,
        )
        self.assertTrue(ui_instances[0].stopped.is_set())

    async def test_noninteractive_chat_streams_and_prints_resume_command(self) -> None:
        class FakeRun:
            def events(self):
                async def event_stream():
                    yield MessageUpdateEvent(
                        assistant_message_event=ContentDeltaEvent(
                            type="text_delta",
                            content_index=0,
                            delta="assistant response",
                        )
                    )

                return event_stream()

            async def result(self):
                return SimpleNamespace(model_calls=1, tool_calls=0)

        client = SimpleNamespace(
            session=SimpleNamespace(id="session-2", messages=(object(),)),
            extensions=SimpleNamespace(commands=()),
            run=lambda _prompt: FakeRun(),
        )
        output = StringIO()

        with (
            patch("sec.apps.cli.app.sys.stdin", StringIO("hello\n/exit\n")),
            redirect_stdout(output),
        ):
            await _chat_noninteractive(client)

        self.assertIn("assistant response", output.getvalue())
        self.assertIn("sec --session session-2", output.getvalue())


if __name__ == "__main__":
    unittest.main()
