"""验证 CLI 内置工具的工作区边界、权限模式和 Bash 限制。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from sec._types.policy import (
    PolicyCapabilityType,
    PolicyContext,
    PolicyRiskLevel,
)
from sec.policies.modes import BUILTIN_WORKSPACE_SOURCE, PermissionModePolicy
from sec.tools._builtins import create_builtin_registry, create_builtin_tools


class BuiltinToolTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_tools_are_registered_in_their_existing_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = create_builtin_registry(temporary_directory)

            self.assertEqual(
                tuple(tool.definition.name for tool in registry.tools),
                ("read", "write", "edit", "ls", "find", "grep", "bash"),
            )
            self.assertEqual(
                tuple(
                    (
                        tool.risk_level,
                        tool.source,
                        tool.workspace_scoped,
                    )
                    for tool in registry.tools
                ),
                (
                    ("read", BUILTIN_WORKSPACE_SOURCE, True),
                    ("write", BUILTIN_WORKSPACE_SOURCE, True),
                    ("write", BUILTIN_WORKSPACE_SOURCE, True),
                    ("read", BUILTIN_WORKSPACE_SOURCE, True),
                    ("read", BUILTIN_WORKSPACE_SOURCE, True),
                    ("read", BUILTIN_WORKSPACE_SOURCE, True),
                    ("execute", "sec.builtin.shell", False),
                ),
            )
            self.assertIs(registry.get("read"), registry.tools[0])
            self.assertEqual(
                tuple(definition.name for definition in registry.definitions()),
                ("read", "write", "edit", "ls", "find", "grep", "bash"),
            )

    async def test_workspace_file_tools_read_write_edit_search_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "sample.txt").write_text("first\nneedle here\n", encoding="utf-8")
            tools = {
                tool.definition.name: tool
                for tool in create_builtin_tools(root)
            }

            read_result = await tools["read"].invoke({"path": "sample.txt"})
            grep_result = await tools["grep"].invoke({"pattern": "needle"})
            find_result = await tools["find"].invoke({"pattern": "*.txt"})
            list_result = await tools["ls"].invoke({})
            write_result = await tools["write"].invoke(
                {"path": "nested/new.txt", "content": "before after"}
            )
            edit_result = await tools["edit"].invoke(
                {
                    "path": "nested/new.txt",
                    "old_text": "before",
                    "new_text": "after",
                }
            )

            self.assertIn("2: needle here", read_result)
            self.assertEqual(grep_result[0]["line"], 2)
            self.assertIn("sample.txt", find_result)
            self.assertTrue(any(item["name"] == "sample.txt" for item in list_result))
            self.assertIn("Wrote nested/new.txt", write_result)
            self.assertIn("Edited nested/new.txt", edit_result)
            self.assertEqual(
                (root / "nested/new.txt").read_text(encoding="utf-8"),
                "after after",
            )

    async def test_workspace_tools_reject_escape_and_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspace"
            root.mkdir()
            outside = Path(temporary_directory) / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            (root / ".env").write_text("secret", encoding="utf-8")
            tools = {
                tool.definition.name: tool
                for tool in create_builtin_tools(root)
            }

            with self.assertRaises(PermissionError):
                await tools["read"].invoke({"path": "../outside.txt"})
            with self.assertRaises(PermissionError):
                await tools["read"].invoke({"path": ".env"})
            self.assertNotIn(".env", [item["name"] for item in tools["ls"].function(".")])

    async def test_bash_returns_exit_status_and_captured_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tools = {
                tool.definition.name: tool
                for tool in create_builtin_tools(temporary_directory)
            }

            result = await tools["bash"].invoke({"command": "printf 'hello'"})

            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["stdout"], "hello")
            self.assertEqual(result["stderr"], "")
            self.assertFalse(result["timed_out"])

    async def test_workspace_tools_reject_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspace"
            root.mkdir()
            outside = Path(temporary_directory) / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            (root / "outside-link").symlink_to(outside)
            tools = {
                tool.definition.name: tool
                for tool in create_builtin_tools(root)
            }

            with self.assertRaises(PermissionError):
                await tools["read"].invoke({"path": "outside-link"})
            with self.assertRaises(PermissionError):
                await tools["write"].invoke(
                    {"path": "outside-link", "content": "changed"}
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "private")

    async def test_bash_timeout_terminates_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            tools = {
                tool.definition.name: tool
                for tool in create_builtin_tools(temporary_directory)
            }
            with patch("sec.tools._builtins.bash._BASH_TIMEOUT_SECONDS", 0.01):
                result = await asyncio.wait_for(
                    tools["bash"].invoke({"command": "sleep 5"}),
                    timeout=2,
                )

            self.assertTrue(result["timed_out"])
            self.assertNotEqual(result["exit_code"], 0)


class PermissionModeTests(unittest.TestCase):
    def test_modes_apply_user_selected_risk_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            policy = PermissionModePolicy("plan", workspace_root=root)
            read_context = _context(risk_level="read")
            write_context = _context(risk_level="write")
            bash_context = _context(
                source="sec.builtin.shell",
                risk_level="execute",
                workspace_scoped=False,
            )
            mcp_context = _context(
                capability_type="mcp",
                source="remote:mcp",
                risk_level="network",
                workspace_scoped=False,
            )

            self.assertEqual(policy.decide(read_context).outcome, "allow")
            self.assertEqual(policy.decide(write_context).outcome, "deny")

            policy.set_mode("normal")
            self.assertEqual(policy.decide(read_context).outcome, "ask")
            self.assertEqual(policy.decide(write_context).outcome, "ask")

            policy.set_mode("auto")
            self.assertEqual(policy.decide(read_context).outcome, "allow")
            self.assertEqual(policy.decide(write_context).outcome, "allow")
            self.assertEqual(policy.decide(bash_context).outcome, "ask")
            self.assertEqual(policy.decide(mcp_context).outcome, "ask")

    def test_all_modes_deny_workspace_escape_and_sensitive_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "workspace"
            root.mkdir()
            outside = Path(temporary_directory) / "outside.txt"
            policy = PermissionModePolicy("auto", workspace_root=root)

            escape = _context(
                arguments={"path": str(outside)},
                risk_level="read",
            )
            sensitive = _context(
                arguments={"path": ".env"},
                risk_level="read",
            )

            self.assertEqual(policy.decide(escape).outcome, "deny")
            self.assertEqual(policy.decide(sensitive).outcome, "deny")


def _context(
    *,
    capability_type: PolicyCapabilityType = "tool",
    source: str = BUILTIN_WORKSPACE_SOURCE,
    risk_level: PolicyRiskLevel,
    workspace_scoped: bool = True,
    arguments: dict[str, str] | None = None,
) -> PolicyContext:
    return PolicyContext(
        agent_name="sec",
        mode_name=None,
        capability_type=capability_type,
        capability_name="read",
        source=source,
        operation="read",
        arguments=arguments or {"path": "source.py"},
        risk_level=risk_level,
        workspace_scoped=workspace_scoped,
    )


if __name__ == "__main__":
    unittest.main()
