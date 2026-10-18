from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

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


if __name__ == "__main__":
    unittest.main()
