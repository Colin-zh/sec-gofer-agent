"""Exercise worker reload and RPC with real installed-entry-point subprocesses."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import py_compile
import shutil
import sys
import textwrap
import unittest
import uuid
from unittest.mock import patch

from sec.apis import AsyncSecClient
from sec._types.content import TextContent, ToolUseContent
from sec._types.messages import AssistantMessage, Usage, UsageCost
from sec.policies.rules import AllowListPolicy

from sec.extensions.worker import WorkerExtensionError, WorkerExtensionManager
from sec.tools.tool import ToolOutput


class WorkerExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path.cwd() / f".worker-fixtures-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.module_name = f"worker_plugin_{uuid.uuid4().hex}"
        self.module = self.root / f"{self.module_name}.py"
        self.closed = self.root / "closed"
        self.ignored = self.root / "ignored"
        self._write_plugin()
        (self.root / "never_import_worker.py").write_text(
            f"from pathlib import Path\nPath({str(self.ignored)!r}).touch()\n"
            "raise RuntimeError('unselected entry point was imported')\n",
            encoding="utf-8",
        )
        metadata = self.root / "sec_worker_fixture-1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: sec-worker-fixture\nVersion: 1.0\n",
            encoding="utf-8",
        )
        (metadata / "entry_points.txt").write_text(
            f"[sec.extensions]\nsample = {self.module_name}:extension\n"
            f"other = {self.module_name}:extension\n"
            f"fixture = {self.module_name}:extension\n"
            "ignored = never_import_worker:extension\n",
            encoding="utf-8",
        )
        paths = [str(self.root), str(Path.cwd())]
        if os.environ.get("PYTHONPATH"):
            paths.append(os.environ["PYTHONPATH"])
        self.env = {
            "PYTHONPATH": os.pathsep.join(paths),
            "SEC_WORKER_TEST_SECRET": "private-credential-value",
        }
        self.managers: list[WorkerExtensionManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.aclose()
        shutil.rmtree(self.root)

    def manager(self, **kwargs) -> WorkerExtensionManager:
        manager = WorkerExtensionManager(env=self.env, shutdown_timeout=0.5, **kwargs)
        self.managers.append(manager)
        return manager

    def _write_plugin(self, *, value: str = "one", activation: str = "", close: str = "") -> None:
        self.module.write_text(
            textwrap.dedent(
                f"""\
                import asyncio
                import os
                from pathlib import Path
                from sec.tools import tool
                print("plugin import chatter", flush=True)
                os.write(1, b"plugin fd chatter\\n")
                VALUE = {value!r}

                @tool(name="echo", description="Echo from a worker.", retry_safe=True,
                      risk_level="read", source="extension:sample", workspace_scoped=True)
                async def echo(value: str, delay: float = 0.0) -> str:
                    print("tool chatter", flush=True)
                    await asyncio.sleep(delay)
                    if value == "fail":
                        raise RuntimeError(os.environ["SEC_WORKER_TEST_SECRET"])
                    if value == "crash":
                        os._exit(19)
                    return VALUE + ":" + value

                class Extension:
                    async def activate(self, api):
                        api.register_tool(echo)
                        {activation or "pass"}

                    async def deactivate(self):
                        {close or f"Path({str(self.closed)!r}).open('a').write(str(os.getpid()) + chr(10))"}

                extension = Extension()
                """
            ),
            encoding="utf-8",
        )

    async def test_client_resource_reload_uses_fresh_worker_preserving_session(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        def response(*, call_id=None):
            return AssistantMessage(
                content=(
                    [TextContent("done")]
                    if call_id is None
                    else [ToolUseContent(call_id, "echo", {"value": "hello"})]
                ),
                api="test", provider="test", model="test",
                usage=Usage(1, 1, 0, 0, 2, UsageCost(0, 0, 0, 0, 0)),
                stop_reason="stop" if call_id is None else "tool_use",
                timestamp=1, metadata={"usage_reported": True},
            )

        class Provider:
            async def complete(provider, messages, tools):
                requests.append((tuple(messages), tuple(tools)))
                if len(requests) == 1:
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=20)
                    return response(call_id="old")
                if len(requests) == 2:
                    return response(call_id="fresh")
                return response()

            async def aclose(provider):
                pass

        home = self.root / "home"
        home.mkdir()
        workspace = self.root / "workspace"
        workspace.mkdir()
        with patch.dict(os.environ, self.env), patch(
            "pathlib.Path.home", return_value=home,
        ), patch("sec.apis.client.create_provider", return_value=Provider()):
            async with AsyncSecClient(
                "assistant", "Use echo.", workspace=workspace,
                worker_extensions=("fixture",), provider="openai", model="test",
                api_key="test", env_file=None, session_store_backend="in_memory",
                policy=AllowListPolicy({"echo"}),
            ) as client:
                session = client.session
                original = next(t for t in client.available_tools if t.definition.name == "echo")
                self.assertEqual(original.source, "worker/fixture")
                run = client.run("Echo hello before and after reload.")
                result_task = asyncio.create_task(run.result())
                entered_task = asyncio.create_task(entered.wait())
                try:
                    done, _ = await asyncio.wait(
                        (result_task, entered_task), timeout=10,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if result_task in done:
                        await result_task
                        self.fail("run completed before the provider request")
                    self.assertTrue(entered.is_set(), "provider request did not start")
                    self._write_plugin(value="two")
                    report = await asyncio.wait_for(client.reload_resources(), timeout=15)
                    self.assertTrue(report.pending)
                    current = next(t for t in client.available_tools if t.definition.name == "echo")
                    self.assertIsNot(current, original)
                    self.assertEqual(await original.invoke({"value": "snapshot"}), "one:snapshot")
                    self.assertFalse(self.closed.exists(), "old root snapshot must retain its worker")
                finally:
                    release.set()
                    entered_task.cancel()
                    await asyncio.gather(entered_task, return_exceptions=True)
                result = await asyncio.wait_for(result_task, timeout=15)
                self.assertEqual(result.text, "done")
                self.assertIs(client.session, session)
                outputs = [message.text for message in session.messages if message.role == "tool_result"]
                self.assertEqual(outputs, ["one:hello", "two:hello"])
                self.assertEqual(requests[1][0][-1].text, "one:hello")
                self.assertEqual(requests[2][0][-1].text, "two:hello")
                self.assertTrue(self.closed.exists(), "old manager must close when root drains")
                self.assertEqual(await current.invoke({"value": "current"}), "two:current")

    async def test_fresh_source_reload_retains_old_bindings_and_metadata(self) -> None:
        # Reproduce timestamp-pyc staleness: same size and identical mtime.
        py_compile.compile(str(self.module), doraise=True)
        old_stat = self.module.stat()
        manager = self.manager()
        old, = await manager.reload(("sample",))
        self.assertEqual(await old.invoke({"value": "hello"}), "one:hello")
        generation = manager.generation
        first_pid = manager.statuses[0].pid
        self._write_plugin(value="two")
        self.assertEqual(self.module.stat().st_size, old_stat.st_size)
        os.utime(self.module, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        current, = await manager.reload(("sample",))
        self.assertEqual(await current.invoke({"value": "hello"}), "two:hello")
        self.assertEqual(await old.invoke({"value": "hello"}), "one:hello")
        self.assertNotEqual(first_pid, manager.statuses[0].pid)
        self.assertGreater(manager.generation, generation)
        self.assertNotIn(self.module_name, sys.modules)
        self.assertFalse(self.ignored.exists())
        self.assertEqual(manager.enabled, ("sample",))
        self.assertEqual(manager.tools, (current,))
        self.assertEqual(manager.statuses[0].tools, ("echo",))
        self.assertEqual(current.definition, old.definition)
        self.assertTrue(current.retry_safe)
        self.assertEqual(current.risk_level, "read")
        self.assertEqual(current.source, "worker/sample")
        self.assertTrue(current.workspace_scoped)
        self.assertEqual(current.capability_type, "tool")

    async def test_failed_reload_preserves_previous_generation(self) -> None:
        manager = self.manager()
        original = await manager.reload(("sample",))
        statuses = manager.statuses
        generation = manager.generation
        self._write_plugin(activation="raise RuntimeError(os.environ['SEC_WORKER_TEST_SECRET'])")
        with self.assertRaises(WorkerExtensionError) as error:
            await manager.reload(("sample",))
        self.assertNotIn(self.env["SEC_WORKER_TEST_SECRET"], str(error.exception))
        self.assertEqual(manager.tools, original)
        self.assertEqual(manager.statuses, statuses)
        self.assertEqual(manager.generation, generation)
        self.assertEqual(manager.enabled, ("sample",))
        self.assertEqual(await original[0].invoke({"value": "ok"}), "one:ok")
        self.assertTrue(self.closed.exists(), "candidate must be deactivated")

    async def test_import_errors_are_distinct_from_activation_and_preserve_old_worker(self) -> None:
        manager = self.manager()
        original = await manager.reload(("sample",))
        statuses = manager.statuses
        generation = manager.generation
        for exception, message in (
            ("ModuleNotFoundError", "plugin module or one of its dependencies was not found"),
            ("ImportError", "plugin import failed"),
        ):
            with self.subTest(exception=exception):
                self.module.write_text(
                    f"raise {exception}('private-credential-value')\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(WorkerExtensionError, message) as error:
                    await manager.reload(("sample",))
                self.assertNotIn("private-credential-value", str(error.exception))
                self.assertEqual(manager.tools, original)
                self.assertEqual(manager.statuses, statuses)
                self.assertEqual(manager.generation, generation)
                self.assertEqual(await original[0].invoke({"value": "ok"}), "one:ok")

    async def test_all_unsupported_contributions_fail_explicitly(self) -> None:
        methods = {
            "register_provider('remote', lambda config: None)": "provider",
            "register_policy('remote', object())": "policy",
            "register_storage_backend('remote', lambda config, db: None)": "storage",
            "register_ui_handler('remote', lambda request: None)": "UI",
            "register_command(object())": "command",
            "register_before_run_hook(lambda context: None)": "hook",
            "register_after_run_hook(lambda context: None)": "hook",
            "register_run_event_hook(lambda event: None)": "hook",
        }
        manager = self.manager()
        for registration, category in methods.items():
            with self.subTest(registration=registration):
                self._write_plugin(activation=f"api.{registration}")
                with self.assertRaisesRegex(
                    WorkerExtensionError, f"unsupported {category}.*tools-only"
                ):
                    await manager.reload(("sample",))
                self.assertEqual(manager.tools, ())
                self.assertEqual(manager.enabled, ())

    async def test_concurrent_calls_are_correlated_out_of_order(self) -> None:
        manager = self.manager()
        registered, = await manager.reload(("sample",))
        values = [str(number) for number in range(12)]
        results = await asyncio.gather(
            *(
                registered.invoke({"value": value, "delay": (12 - index) / 100})
                for index, value in enumerate(values)
            )
        )
        self.assertEqual(results, [f"one:{value}" for value in values])

    async def test_cancelled_call_does_not_poison_other_requests(self) -> None:
        manager = self.manager()
        registered, = await manager.reload(("sample",))
        slow = asyncio.create_task(registered.invoke({"value": "slow", "delay": 5.0}))
        await asyncio.sleep(0.05)
        slow.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await slow
        self.assertEqual(await registered.invoke({"value": "next"}), "one:next")

    async def test_invocation_errors_do_not_expose_credentials(self) -> None:
        manager = self.manager()
        registered, = await manager.reload(("sample",))
        with self.assertRaisesRegex(WorkerExtensionError, "tool invocation failed") as error:
            await registered.invoke({"value": "fail"})
        self.assertNotIn(self.env["SEC_WORKER_TEST_SECRET"], str(error.exception))
        self.assertEqual(await registered.invoke({"value": "ok"}), "one:ok")

    async def test_crashed_worker_fails_all_pending_calls_and_can_reload(self) -> None:
        manager = self.manager()
        registered, = await manager.reload(("sample",))
        results = await asyncio.gather(
            registered.invoke({"value": "slow", "delay": 60.0}),
            registered.invoke({"value": "crash"}),
            return_exceptions=True,
        )
        self.assertTrue(all(isinstance(result, WorkerExtensionError) for result in results))
        self.assertEqual(manager.statuses[0].state, "failed")
        replacement, = await manager.reload(("sample",))
        self.assertEqual(await replacement.invoke({"value": "ok"}), "one:ok")
        self.assertEqual(manager.statuses[0].state, "ready")

    async def test_timeout_cancels_request_and_keeps_worker_usable(self) -> None:
        manager = self.manager(timeout=5.0)
        registered, = await manager.reload(("sample",))
        with self.assertRaisesRegex(WorkerExtensionError, "timed out"):
            await registered.invoke({"value": "slow", "delay": 60.0})
        self.assertEqual(await registered.invoke({"value": "next"}), "one:next")

    async def test_schema_tools_round_trip_nested_arguments_and_output(self) -> None:
        self.module.write_text(
            textwrap.dedent(
                """\
                from sec.tools.tool import Tool, ToolOutput
                SCHEMA = {
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "properties": {"items": {"type": "array", "items": {"type": ["string", "null"]}}},
                            "required": ["items"],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["payload"],
                    "additionalProperties": False,
                }
                async def call(arguments):
                    if arguments["payload"]["items"] == ["output"]:
                        return ToolOutput("done", {"ok": True}, {"trace": 1}, True)
                    return arguments
                class Extension:
                    def activate(self, api):
                        api.register_tool(Tool.from_schema(
                            call, name="nested", description="Nested schema.",
                            input_schema=SCHEMA, output_schema=SCHEMA,
                            risk_level="network", capability_type="mcp",
                        ))
                    def deactivate(self):
                        pass
                extension = Extension()
                """
            ),
            encoding="utf-8",
        )
        manager = self.manager()
        registered, = await manager.reload(("sample",))
        arguments = {"payload": {"items": ["value", None]}}
        self.assertEqual(await registered.invoke(arguments), arguments)
        output = await registered.invoke({"payload": {"items": ["output"]}})
        self.assertIsInstance(output, ToolOutput)
        self.assertEqual(output, ToolOutput("done", {"ok": True}, {"trace": 1}, True))
        self.assertEqual(registered.definition.input_schema, registered.definition.output_schema)
        self.assertEqual(registered.risk_level, "network")
        self.assertEqual(registered.source, "worker/sample")
        self.assertEqual(registered.capability_type, "tool")

    async def test_duplicate_tools_roll_back_all_candidates(self) -> None:
        manager = self.manager()
        old, = await manager.reload(("sample",))
        with self.assertRaisesRegex(WorkerExtensionError, "conflicts"):
            await manager.reload(("sample", "other"))
        self.assertEqual(manager.tools, (old,))
        self.assertEqual(await old.invoke({"value": "ok"}), "one:ok")
        self.assertEqual(len(self.closed.read_text().splitlines()), 2)

    async def test_missing_entry_point_and_empty_reload(self) -> None:
        manager = self.manager()
        self.assertEqual(await manager.reload(()), ())
        old, = await manager.reload(("sample",))
        with self.assertRaisesRegex(WorkerExtensionError, "entry point"):
            await manager.reload(("missing",))
        self.assertEqual(manager.tools, (old,))
        self.assertEqual(await manager.reload(()), ())
        self.assertEqual(manager.enabled, ())
        self.assertEqual(await old.invoke({"value": "still-live"}), "one:still-live")
        self.assertFalse(self.ignored.exists())

    async def test_release_obsolete_generation_and_clean_close(self) -> None:
        manager = self.manager()
        old, = await manager.reload(("sample",))
        old_generation = manager.generation
        old_pid = manager.statuses[0].pid
        current, = await manager.reload(("sample",))
        current_pid = manager.statuses[0].pid
        with self.assertRaisesRegex(ValueError, "currently published"):
            await manager.release_generation(manager.generation)
        await manager.release_generation(old_generation)
        with self.assertRaises(ProcessLookupError):
            os.kill(old_pid, 0)
        with self.assertRaisesRegex(WorkerExtensionError, "closed"):
            await old.invoke({"value": "no"})
        self.assertEqual(await current.invoke({"value": "yes"}), "one:yes")
        await manager.aclose()
        await manager.aclose()
        with self.assertRaises(ProcessLookupError):
            os.kill(current_pid, 0)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await manager.reload(("sample",))
        self.assertEqual(manager.tools, ())
        self.assertEqual(manager.statuses, ())
        self.assertEqual(len(self.closed.read_text().splitlines()), 2)

    async def test_close_terminates_a_hung_deactivation(self) -> None:
        self._write_plugin(close="await asyncio.sleep(60)")
        manager = self.manager()
        registered, = await manager.reload(("sample",))
        pid = manager.statuses[0].pid
        pending = asyncio.create_task(registered.invoke({"value": "slow", "delay": 60.0}))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(manager.aclose(), 5.0)
        with self.assertRaises(WorkerExtensionError):
            await pending
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_close_reaps_all_generations_without_explicit_release(self) -> None:
        manager = self.manager()
        await manager.reload(("sample",))
        first = manager.statuses[0].pid
        await manager.reload(("sample",))
        second = manager.statuses[0].pid
        await manager.aclose()
        for pid in (first, second):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertEqual(len(self.closed.read_text().splitlines()), 2)

    async def test_cancelling_close_still_reaps_workers(self) -> None:
        self._write_plugin(close="await asyncio.sleep(60)")
        manager = self.manager()
        await manager.reload(("sample",))
        pid = manager.statuses[0].pid
        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0.05)
        closing.cancel()
        await asyncio.sleep(0.05)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        await manager.aclose()

    async def test_cancelled_reload_reaps_candidate_and_preserves_old(self) -> None:
        manager = self.manager()
        old, = await manager.reload(("sample",))
        started = self.root / "activating"
        self._write_plugin(
            activation=(
                f"Path({str(started)!r}).write_text(str(os.getpid())); "
                "await asyncio.sleep(60)"
            )
        )
        reload = asyncio.create_task(manager.reload(("sample",)))
        async with asyncio.timeout(10):
            while not started.exists():
                await asyncio.sleep(0.02)
        candidate_pid = int(started.read_text())
        reload.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reload
        self.assertEqual(manager.tools, (old,))
        self.assertEqual(await old.invoke({"value": "ok"}), "one:ok")
        with self.assertRaises(ProcessLookupError):
            os.kill(candidate_pid, 0)


if __name__ == "__main__":
    unittest.main()
