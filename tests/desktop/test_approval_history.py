import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sec import AsyncSecClient, PermissionModePolicy, tool
from sec._types.content import TextContent, ToolUseContent
from sec._types.session import CustomMessageEntry
from sec.apps.desktop.runtime import SecRuntime
from test_policy_executor import SequenceProvider, _assistant_message


class ApprovalHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_audit_save_never_authorizes_tool(self) -> None:
        runtime = SecRuntime(Path.cwd())
        run = Mock(done=False)
        requested = asyncio.Event()
        runtime.approval_requested.connect(lambda *_args: requested.set())
        with (
            patch.object(runtime, "_record_approval", new_callable=AsyncMock) as save,
            patch.object(runtime, "_publish_session_history"),
        ):
            save.side_effect = [None, OSError("disk full")]
            task = asyncio.create_task(
                runtime._request_approval(run, "call", "tool", {}, None),
            )
            await asyncio.wait_for(requested.wait(), 3)
            runtime._resolve_approval("call", True)
            with self.assertRaisesRegex(OSError, "disk full"):
                await asyncio.wait_for(task, 3)
        run.approve_tool.assert_not_called()
        run.deny_tool.assert_not_called()

    async def test_approval_choices_and_cancel_are_persisted_and_restored_without_actions(self) -> None:
        for decision in ("allow", "deny", "cancel"):
            with self.subTest(decision=decision), TemporaryDirectory() as directory:
                calls = []

                @tool
                def desktop_probe(value: str) -> str:
                    """Return the test value."""
                    calls.append(value)
                    return value

                provider = SequenceProvider(
                    _assistant_message([ToolUseContent("call-1", "desktop_probe", {"value": "ok"})]),
                    _assistant_message([TextContent("Finished")]),
                )
                with patch("sec.apis.client.create_provider", return_value=provider):
                    async with AsyncSecClient(
                        "desktop-audit", "", provider="openai", model="test", api_key="test",
                        env_file=None, session_store_backend="database",
                        database_url=f"sqlite+aiosqlite:///{Path(directory) / 'sessions.db'}",
                        tools=(desktop_probe,),
                        policy=PermissionModePolicy("normal", workspace_root=directory),
                    ) as client:
                        runtime = SecRuntime(Path(directory))
                        runtime._client = client
                        requested = asyncio.Event()
                        runtime.approval_requested.connect(lambda *_args: requested.set())
                        history = []
                        timing = []
                        errors = []
                        runtime.session_history.connect(history.append)
                        runtime.session_timing.connect(timing.append)
                        runtime.run_failed.connect(errors.append)
                        task = asyncio.create_task(runtime._run_prompt("Call the tool"))
                        try:
                            await asyncio.wait_for(requested.wait(), 3)
                            records = [
                                e for e in client.session.entries if isinstance(e, CustomMessageEntry)
                            ]
                            self.assertEqual(records[-1].details["outcome"], "requested")
                            self.assertFalse(task.done())
                            self.assertEqual(calls, [])
                            if decision == "cancel":
                                runtime._cancel_run()
                            else:
                                runtime._resolve_approval("wrong-id", True)
                                self.assertFalse(runtime._approval_future.done())
                                runtime._resolve_approval("call-1", decision == "allow")
                            await asyncio.wait_for(task, 3)
                            self.assertEqual(errors, [])
                            self.assertEqual(len(timing[-1]), len(history[-1]))
                            self.assertTrue(all(isinstance(value, (int, float)) for value in timing[-1]))
                            self.assertEqual(calls, ["ok"] if decision == "allow" else [])
                            outcomes = [
                                e.details["outcome"] for e in client.session.entries
                                if isinstance(e, CustomMessageEntry)
                            ]
                            expected = {"allow": "approved", "deny": "denied", "cancel": "cancelled"}[decision]
                            self.assertEqual(outcomes, ["requested", expected])
                            session_id = client.session.id
                            await client.switch_session()
                            await client.switch_session(session_id)
                            runtime._publish_session_history()
                            notices = "\n".join(text for role, text in history[-1] if role == "activity")
                            self.assertIn("历史请求", notices)
                            self.assertIn("desktop_probe", notices)
                            if decision != "cancel":
                                self.assertIn("工具完成" if decision == "allow" else "工具失败", notices)
                            self.assertTrue(all(
                                message.role != "system" for message in client.session.messages
                            ))
                        finally:
                            if not task.done():
                                runtime._cancel_run()
                                await asyncio.wait_for(task, 3)
                    async with AsyncSecClient(
                        "desktop-audit", "", provider="openai", model="test", api_key="test",
                        env_file=None, session_store_backend="database",
                        database_url=f"sqlite+aiosqlite:///{Path(directory) / 'sessions.db'}",
                    ) as reopened:
                        await reopened.switch_session(session_id)
                        restored_runtime = SecRuntime(Path(directory))
                        restored_runtime._client = reopened
                        restored_history = []
                        restored_runtime.session_history.connect(restored_history.append)
                        restored_runtime._publish_session_history()
                        text = "\n".join(item[1] for item in restored_history[-1])
                        self.assertIn("历史请求", text)
                        self.assertIn(
                            {"allow": "审批允许", "deny": "审批拒绝", "cancel": "审批已取消"}[decision],
                            text,
                        )
