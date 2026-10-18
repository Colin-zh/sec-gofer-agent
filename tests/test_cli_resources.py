"""验证 Markdown 资源发现及聊天命令执行。"""

from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sec.apps.cli.app import _chat_noninteractive, _create_commands
from sec.apps.cli.resources import CliResourceManager
from sec.apps.cli.terminal import TerminalUI


class CliResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_resources_override_user_resources(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user_skill = root / "home" / "skills" / "review" / "SKILL.md"
            workspace_skill = root / "workspace" / ".sec" / "skills" / "review" / "SKILL.md"
            prompt = root / "workspace" / ".sec" / "prompts" / "summarize.md"
            user_skill.parent.mkdir(parents=True)
            workspace_skill.parent.mkdir(parents=True)
            prompt.parent.mkdir(parents=True)
            user_skill.write_text(
                "---\nname: review\ndescription: user review\n---\nUser rules.",
                encoding="utf-8",
            )
            workspace_skill.write_text(
                "---\nname: review\ndescription: workspace review\n---\nWorkspace rules.",
                encoding="utf-8",
            )
            prompt.write_text("# Summarize\nWrite a concise summary.", encoding="utf-8")

            manager = CliResourceManager(
                root / "workspace",
                user_root=root / "home",
            )
            snapshot = manager.reload()

            self.assertEqual(len(snapshot.skills), 1)
            self.assertEqual(snapshot.skills[0].content, "Workspace rules.")
            self.assertEqual(snapshot.skills[0].description, "workspace review")
            self.assertEqual(snapshot.prompts[0].name, "summarize")
            self.assertTrue(any("overridden" in issue.message for issue in snapshot.issues))

    async def test_resource_commands_run_loaded_skill_and_reload_completions(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            skill = workspace / ".sec" / "skills" / "inspect" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: inspect\ndescription: Inspect a codebase\n---\n"
                "Inspect safely.",
                encoding="utf-8",
            )
            manager = CliResourceManager(workspace, user_root=workspace / "home")
            manager.reload()
            client = SimpleNamespace(
                extensions=SimpleNamespace(commands=()),
                enabled_extensions=(),
                discover_extensions=lambda: (),
            )
            commands = _create_commands(client, resource_manager=manager)

            outcome = await commands.dispatch("/skill:inspect check the API")

            self.assertIn("Inspect safely.", outcome.run_prompt)
            self.assertIn("check the API", outcome.run_prompt)
            self.assertIn(("/skill:inspect", "Inspect a codebase"), commands.completion_items())

            prompt = workspace / ".sec" / "prompts" / "release-note.md"
            prompt.parent.mkdir(parents=True, exist_ok=True)
            prompt.write_text("Write release notes.", encoding="utf-8")
            reload_result = await commands.dispatch("/reload")

            self.assertIn("1 Skill(s) and 1 Prompt(s)", reload_result.message)
            self.assertIn("Installed Extensions", reload_result.message)
            self.assertIn("/prompt:release-note", {item[0] for item in commands.completion_items()})

    async def test_reload_can_enable_list_and_disable_extensions(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)

            class FakeClient:
                def __init__(self) -> None:
                    self.extensions = SimpleNamespace(commands=())
                    self._enabled: tuple[str, ...] = ()

                @property
                def enabled_extensions(self) -> tuple[str, ...]:
                    return self._enabled

                def discover_extensions(self) -> tuple[str, ...]:
                    return ("dynamic",)

                async def set_enabled_extensions(self, enabled: tuple[str, ...]) -> None:
                    self._enabled = enabled
                    if "dynamic" in enabled:
                        self.extensions.commands = (
                            SimpleNamespace(
                                name="dynamic-command",
                                description="A dynamically loaded command",
                                handler=lambda _args: 0,
                            ),
                        )
                    else:
                        self.extensions.commands = ()

            client = FakeClient()
            manager = CliResourceManager(workspace, user_root=workspace / "home")
            commands = _create_commands(client, resource_manager=manager)

            discovered = await commands.dispatch("/reload")
            self.assertIn("dynamic (disabled)", discovered.message)

            enabled = await commands.dispatch("/reload enable dynamic")
            self.assertEqual(client.enabled_extensions, ("dynamic",))
            self.assertIn("/dynamic-command", {
                item[0] for item in commands.completion_items()
            })
            self.assertIn("Enabled dynamic.", enabled.message)
            self.assertEqual(
                (await commands.dispatch("/dynamic-command")).display_kind,
                "info",
            )

            disabled = await commands.dispatch("/reload disable dynamic")
            self.assertEqual(client.enabled_extensions, ())
            self.assertNotIn("/dynamic-command", {
                item[0] for item in commands.completion_items()
            })
            self.assertIn("Disabled dynamic.", disabled.message)
            self.assertEqual(
                (await commands.dispatch("/dynamic-command")).display_kind,
                "error",
            )

    async def test_reload_restores_previous_set_on_command_name_conflict(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)

            class FakeClient:
                def __init__(self) -> None:
                    self.extensions = SimpleNamespace(commands=())
                    self._enabled: tuple[str, ...] = ()

                @property
                def enabled_extensions(self) -> tuple[str, ...]:
                    return self._enabled

                def discover_extensions(self) -> tuple[str, ...]:
                    return ("conflict",)

                async def set_enabled_extensions(self, enabled: tuple[str, ...]) -> None:
                    self._enabled = enabled
                    self.extensions.commands = (
                        (
                            SimpleNamespace(
                                name="reload",
                                description="Conflicting command",
                                handler=lambda _args: 0,
                            ),
                        )
                        if "conflict" in enabled
                        else ()
                    )

            client = FakeClient()
            manager = CliResourceManager(workspace, user_root=workspace / "home")
            commands = _create_commands(client, resource_manager=manager)

            outcome = await commands.dispatch("/reload enable conflict")

            self.assertEqual(client.enabled_extensions, ())
            self.assertEqual(client.extensions.commands, ())
            self.assertEqual(outcome.display_kind, "error")
            self.assertIn("conflicts with a built-in command", outcome.message)

    async def test_ctrl_o_opens_expanded_help_command(self) -> None:
        ui = TerminalUI(force_terminal=False)
        ui._pending_input.extend(b"\x0f")

        ui._consume_input()

        self.assertEqual(await ui.input_queue.get(), "/help full")

    async def test_noninteractive_skill_command_runs_resource_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            skill = workspace / ".sec" / "skills" / "inspect" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: inspect\ndescription: Inspect code\n---\n"
                "Inspect the repository.",
                encoding="utf-8",
            )
            manager = CliResourceManager(workspace, user_root=workspace / "home")
            manager.reload()
            prompts: list[str] = []

            class FakeRun:
                async def events(self):
                    if False:
                        yield None

                async def result(self):
                    return SimpleNamespace(model_calls=1, tool_calls=0)

            client = SimpleNamespace(
                session=SimpleNamespace(id="session", messages=()),
                extensions=SimpleNamespace(commands=()),
                run=lambda prompt: prompts.append(prompt) or FakeRun(),
            )
            output = StringIO()
            with (
                patch(
                    "sec.apps.cli.app.sys.stdin",
                    StringIO("/skill:inspect check the API\n/exit\n"),
                ),
                redirect_stdout(output),
            ):
                await _chat_noninteractive(client, resource_manager=manager)

            self.assertEqual(len(prompts), 1)
            self.assertIn("Inspect the repository.", prompts[0])
            self.assertIn("check the API", prompts[0])


if __name__ == "__main__":
    unittest.main()
