"""Official SDK transport fixtures; no credentials or external services required."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from sec._types.content import ToolUseContent
from sec.executors.local import LocalToolExecutor
from sec.policies.modes import PermissionModePolicy
from sec.integrations.mcp import (
    MCPConfigurationError,
    MCPConnectionError,
    MCPManager,
    UnsupportedMCPContentError,
    parse_configuration,
    tool_name,
)


FIXTURE = str(Path(__file__).with_name("test_mcp_service.py").resolve())


class MCPConfigurationTests(unittest.TestCase):
    def test_only_explicitly_enabled_servers_are_parsed(self) -> None:
        self.assertEqual(parse_configuration({"servers": {
            "new": {"command": "never-connect"},
            "disabled": {"enabled": False, "transport": "invalid"},
        }}), ())
        with self.assertRaises(MCPConfigurationError):
            parse_configuration({"servers": {"bad": {"enabled": "true"}}})

    def test_environment_references_not_literals(self) -> None:
        with patch.dict(os.environ, {"MCP_TEST_SECRET": "private-token"}):
            configs = parse_configuration({"servers": {"test": {
                "enabled": True, "url": "http://127.0.0.1/mcp",
                "headers": {"Authorization": "Bearer ${MCP_TEST_SECRET}"},
                "env": {"SECRET": {"env": "MCP_TEST_SECRET"}},
            }}})
        self.assertEqual(configs[0].headers["Authorization"], "Bearer private-token")
        self.assertNotIn("private-token", repr(configs[0]))
        with self.assertRaises(MCPConfigurationError):
            parse_configuration({"servers": {"test": {
                "enabled": True, "command": "test", "env": {"SECRET": "literal-secret"},
            }}})
        with self.assertRaises(MCPConfigurationError):
            parse_configuration({"servers": {"test": {
                "enabled": True, "command": "test", "env": {"SECRET": "${SEC_MISSING_ENV_FIXTURE}"},
            }}})

    def test_names_are_stable_bounded_and_collision_resistant(self) -> None:
        pairs = [("one/two", "a.b"), ("one_two", "a.b"), ("界" * 100, "x" * 100)]
        names = [tool_name(*pair) for pair in pairs]
        self.assertEqual(len(set(names)), len(names))
        for pair, name in zip(pairs, names):
            self.assertTrue(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name))
            self.assertEqual(name, tool_name(*pair))

    def test_server_ids_cannot_ambiguously_encode_capability_sources(self) -> None:
        for server_id in ("allowed/hidden", "allowed\\hidden"):
            with self.assertRaises(MCPConfigurationError):
                parse_configuration({"servers": {server_id: {"enabled": False}}})


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.workspace = Path.cwd() / f".mcp-test-{uuid4().hex}"
        self.workspace.mkdir()
        self.manager = MCPManager(self.workspace)
        self.process = None

    async def asyncTearDown(self) -> None:
        try:
            await asyncio.create_task(self.manager.aclose())
        finally:
            if self.process is not None and self.process.returncode is None:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), 10)
            shutil.rmtree(self.workspace)

    def stdio_config(self) -> dict:
        return {"servers": {"fixture": {
            "enabled": True, "transport": "stdio", "command": sys.executable,
            "args": [FIXTURE, "--stdio"], "timeout": 10,
        }, "unknown": {"command": "this-command-must-not-start"}}}

    def find_tool(self, name: str):
        return next(tool for tool in self.manager.tools if tool.definition.name == tool_name("fixture", name))

    async def test_protocol_adapter_uses_client_policy_approval_and_executor(self) -> None:
        from tests.test_capability_runtime import ScriptedProvider, client, response
        name = tool_name("fixture", "sec.resources.read")
        provider = ScriptedProvider([
            response(calls=[ToolUseContent("resource", name, {"uri": "fixture://greeting"})]),
            response("resource inspected"),
        ])
        self.manager.update_configuration(self.stdio_config())
        with patch("sec.apis.client.create_provider", return_value=provider):
            async with client(
                self.workspace,
                policy=PermissionModePolicy("normal", workspace_root=self.workspace),
            ) as active:
                run = active.run("Read the fixture resource")
                approvals = []
                async for event in run.events():
                    if event.type == "tool_approval_requested":
                        approvals.append(event.tool_name)
                        run.approve_tool(event.tool_call_id)
                self.assertEqual((await run.result()).text, "resource inspected")
                self.assertEqual(approvals, [name])
                self.assertIn(name, [definition.name for definition in provider.requests[0][1]])
                self.assertIn("hello resource", str(provider.requests[1][0]))

    async def test_real_server_notification_marks_catalog_dirty(self) -> None:
        self.manager.update_configuration(self.stdio_config())
        await self.manager.reload()
        self.assertFalse(self.manager.dirty)
        await self.find_tool("echo").invoke({"value": "__notify__"})
        async with asyncio.timeout(3):
            while not self.manager.dirty:
                await asyncio.sleep(0.01)
        self.assertTrue(self.manager.statuses[0]["refresh_needed"])
        await self.manager.reload()
        self.assertFalse(self.manager.dirty)

    async def test_stdio_tools_results_resources_and_prompts(self) -> None:
        self.assertEqual(self.manager.tools, ())
        self.manager.update_configuration(self.stdio_config())
        await asyncio.create_task(self.manager.reload())
        echo = self.find_tool("echo")
        self.assertEqual(echo.source, "mcp/fixture")
        self.assertEqual(echo.capability_type, "mcp")
        self.assertEqual(echo.risk_level, "unknown")
        self.assertFalse(echo.retry_safe)
        result = await LocalToolExecutor().execute(
            echo, ToolUseContent("call", echo.definition.name, {"value": "works"})
        )
        self.assertFalse(result.is_error)
        self.assertEqual(result.metadata["structured_content"], {"echo": "works"})
        with self.assertRaises(TypeError):
            await echo.invoke({"value": 123})
        remote_error = self.find_tool("remote_error")
        error = await LocalToolExecutor().execute(
            remote_error, ToolUseContent("error-call", remote_error.definition.name, {})
        )
        self.assertTrue(error.is_error)
        self.assertEqual(error.text, "remote refused")
        self.assertEqual(error.metadata["structured_content"], {"reason": "fixture"})
        self.assertEqual(error.metadata["trace"], "fixture-trace")
        with self.assertRaisesRegex(UnsupportedMCPContentError, "image"):
            await self.find_tool("image").invoke({})
        resources = await self.manager.list_resources()
        self.assertEqual(resources[0]["uri"], "fixture://greeting")
        resource = await self.manager.read_resource("fixture", resources[0]["uri"])
        self.assertEqual(resource["contents"][0]["text"], "hello resource")
        prompts = await self.manager.list_prompts()
        self.assertEqual(prompts[0]["name"], "welcome")
        prompt = await self.manager.get_prompt("fixture", "welcome", {"name": "Sec"})
        self.assertEqual(prompt["messages"][0]["content"]["text"], "Hello Sec")
        self.assertEqual(len(self.manager.tools), 7)
        self.assertNotIn("command", json.dumps(self.manager.statuses))
        statuses = {status["id"]: status for status in self.manager.statuses}
        self.assertEqual(statuses["unknown"]["status"], "disabled")
        self.assertFalse(statuses["unknown"]["enabled"])
        for operation, arguments, expected in (
            ("resources.list", {}, "fixture://greeting"),
            ("resources.read", {"uri": "fixture://greeting"}, "hello resource"),
            ("prompts.list", {}, "welcome"),
            ("prompts.get", {"name": "welcome", "arguments": {"name": "Sec"}}, "Hello Sec"),
        ):
            adapter = self.find_tool(f"sec.{operation}")
            self.assertEqual(adapter.source, "mcp/fixture")
            self.assertEqual(adapter.capability_type, "mcp")
            self.assertFalse(adapter.workspace_scoped)
            output = await LocalToolExecutor().execute(
                adapter, ToolUseContent(operation, adapter.definition.name, arguments),
            )
            self.assertIn(expected, output.text)

    async def test_old_snapshot_survives_reload_and_close_is_cross_task_safe(self) -> None:
        self.manager.update_configuration(self.stdio_config())
        first = await asyncio.create_task(self.manager.reload())
        old = self.find_tool("echo")
        second = await asyncio.create_task(self.manager.reload())
        self.assertEqual([tool.definition.name for tool in first], [tool.definition.name for tool in second])
        self.assertIsNot(first[0], second[0])
        self.manager.update_configuration({"servers": {}})
        self.assertEqual(await asyncio.create_task(self.manager.reload()), ())
        self.assertEqual((await old.invoke({"value": "retained"})).structured_content, {"echo": "retained"})
        await asyncio.create_task(self.manager.aclose())
        await self.manager.aclose()
        with self.assertRaises(MCPConnectionError):
            await old.invoke({"value": "closed"})

    async def test_failed_reload_keeps_published_generation(self) -> None:
        self.manager.update_configuration(self.stdio_config())
        old = await self.manager.reload()
        configuration = self.stdio_config()
        configuration["servers"]["z-broken"] = {
            "enabled": True, "command": str(self.workspace / "missing-command"),
            "timeout": 1,
        }
        self.manager.update_configuration(configuration)
        with self.assertRaises(MCPConnectionError):
            await self.manager.reload()
        self.assertIs(self.manager.tools, old)
        self.assertEqual((await self.find_tool("echo").invoke({"value": "still works"})).structured_content,
                         {"echo": "still works"})

    async def test_streamable_http_tools(self) -> None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, FIXTURE, "--http", str(port),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(10):
            while True:
                if self.process.returncode is not None:
                    self.fail("local HTTP MCP fixture exited")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    await asyncio.sleep(0.05)
                else:
                    writer.close()
                    await writer.wait_closed()
                    break
        self.manager.update_configuration({"servers": {"fixture": {
            "enabled": True, "transport": "streamable_http",
            "url": f"http://127.0.0.1:{port}/mcp",
        }}})
        await asyncio.create_task(self.manager.reload())
        self.assertEqual((await self.find_tool("echo").invoke({"value": "http"})).structured_content,
                         {"echo": "http"})

    async def test_cancelled_prepare_leaves_no_owner_task(self) -> None:
        self.manager.update_configuration({"servers": {"slow": {
            "enabled": True, "command": sys.executable,
            "args": ["-c", "import time; time.sleep(30)"],
        }}})
        pending = asyncio.create_task(self.manager.reload())
        await asyncio.sleep(0.1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.manager.tools, ())
        self.assertEqual(self.manager.statuses, ())
        self.assertFalse(any(
            task.get_name() == "mcp-owner/slow"
            for task in asyncio.all_tasks() if not task.done()
        ))
