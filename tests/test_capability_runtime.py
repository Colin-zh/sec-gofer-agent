"""Acceptance tests for workspace capabilities using real Client/Runtime wiring."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sec import AsyncSecClient
from sec._types.content import TextContent, ToolUseContent
from sec._types.messages import AssistantMessage, Usage, UsageCost
from sec.exceptions import RunLimitExceeded
from sec.policies.rules import AllowListPolicy
from sec.policies.modes import PermissionModePolicy
from sec.tools import tool
from sec._types.policy import PolicyDecision


def response(text: str = "done", calls: list[ToolUseContent] | None = None):
    return AssistantMessage(
        content=[TextContent(text)] if calls is None else list(calls),
        api="test", provider="test", model="test",
        usage=Usage(1, 1, 0, 0, 2, UsageCost(0, 0, 0, 0, 0)),
        stop_reason="stop" if calls is None else "tool_use",
        timestamp=1, metadata={"usage_reported": True},
    )


class ScriptedProvider:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append((tuple(messages), tuple(tools)))
        step = self.steps.pop(0)
        return await step(messages, tools) if callable(step) else step

    async def aclose(self):
        pass


def client(root: Path, **kwargs):
    return AsyncSecClient(
        "assistant", "Follow the user.", workspace=root,
        provider="openai", model="test", api_key="test", env_file=None,
        session_store_backend="in_memory", **kwargs,
    )


def skill(root: Path, name="inspect", content="Inspect carefully.", activation="auto"):
    path = root / ".sec" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Inspect a project\nactivation: {activation}\n"
        f"match: inspect\n---\n{content}", encoding="utf-8",
    )
    return path


def card(root: Path, name: str, extra=""):
    path = root / ".sec" / "agents" / name / "card.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'schema_version = 1\nid = "{name}"\nname = "{name}"\n'
        f'description = "{name} agent"\ninstructions = "Act as {name}."\n'
        f'when_to_use = "When {name} is requested."\n'
        + extra, encoding="utf-8",
    )
    return path


class CapabilityRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        home = TemporaryDirectory()
        self.addCleanup(home.cleanup)
        home_patch = patch("pathlib.Path.home", return_value=Path(home.name))
        home_patch.start()
        self.addCleanup(home_patch.stop)

    async def test_mcp_change_notification_refreshes_at_next_request_without_user_reload(self):
        current = []
        managers = []
        observed = []
        def make_version(version):
            @tool(name="remote", source="mcp/demo", capability_type="mcp")
            def remote() -> str:
                observed.append(version)
                return version
            return remote
        current.append(make_version("one"))
        class Manager:
            def __init__(self, workspace):
                self.tools = ()
                self.statuses = ()
                self.dirty = False
                managers.append(self)
            async def reload(self):
                self.tools = tuple(current)
            async def aclose(self):
                pass
        class Allow:
            def decide(self, context):
                return PolicyDecision.allow()
        async def first(messages, definitions):
            current[:] = [make_version("two")]
            managers[0].dirty = True
            return response(calls=[ToolUseContent("old", "remote", {})])
        provider = ScriptedProvider([
            first, response(calls=[ToolUseContent("new", "remote", {})]), response(),
        ])
        with TemporaryDirectory() as directory:
            with patch("sec.apis.client.create_provider", return_value=provider), patch(
                "sec.integrations.mcp.MCPManager", Manager,
            ):
                async with client(Path(directory), policy=Allow()) as active:
                    await active.run("use remote").result()
                    self.assertEqual(observed, ["one", "two"])
                    self.assertEqual(len(managers), 2)

    async def test_workspace_followup_keeps_existing_per_turn_budget_semantics(self):
        provider = ScriptedProvider([response("first"), response("second")])
        with TemporaryDirectory() as directory:
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(Path(directory), max_model_calls=1) as active:
                    run = active.run("first task")
                    self.assertTrue(run.follow_up("second task"))
                    result = await asyncio.wait_for(run.result(), timeout=5)
                    self.assertEqual(result.text, "second")
                    self.assertEqual(result.model_calls, 2)

    async def test_failed_mcp_enable_restores_persisted_config_and_published_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".sec" / "mcp.json"
            config.parent.mkdir()
            original = {"servers": {"broken": {
                "enabled": False, "transport": "stdio",
                "command": "/definitely/nonexistent/sec-test-mcp",
            }}}
            config.write_text(json.dumps(original), encoding="utf-8")
            provider = ScriptedProvider([response()])
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root) as active:
                    before = active._current_capabilities()
                    with self.assertRaises(Exception):
                        await active.set_mcp_enabled("broken", True)
                    self.assertEqual(json.loads(config.read_text()), original)
                    self.assertIs(active._current_capabilities(), before)
                    await active.run("still usable").result()

    async def test_provider_cannot_mutate_execution_schema_through_request(self):
        values = []
        @tool
        def accept(value: int) -> str:
            values.append(value)
            return str(value)
        async def mutate(messages, definitions):
            definition = next(item for item in definitions if item.name == "accept")
            definition.input_schema["properties"]["value"]["type"] = "string"
            return response(calls=[ToolUseContent("accept", "accept", {"value": 7})])
        provider = ScriptedProvider([mutate, response()])
        with TemporaryDirectory() as directory:
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(
                    Path(directory), tools=(accept,), policy=AllowListPolicy({"accept"}),
                ) as active:
                    await active.run("accept").result()
                    self.assertEqual(values, [7])
                    published = next(t for t in active.available_tools if t.definition.name == "accept")
                    self.assertEqual(
                        published.definition.input_schema["properties"]["value"]["type"], "integer",
                    )

    async def test_large_catalog_exposes_tools_after_native_discovery(self):
        def make_tool(index):
            @tool(name=f"extra_{index}")
            def extra() -> str:
                return str(index)
            return extra
        tools = tuple(make_tool(index) for index in range(25))
        async def first(messages, definitions):
            names = {item.name for item in definitions}
            self.assertNotIn("extra_7", names)
            self.assertIn("capabilities_search", names)
            return response(calls=[ToolUseContent(
                "search", "capabilities_search", {"query": "extra_7"},
            )])
        async def second(messages, definitions):
            names = {item.name for item in definitions}
            self.assertIn("extra_7", names)
            self.assertNotIn("extra_8", names)
            return response(calls=[ToolUseContent("use", "extra_7", {})])
        provider = ScriptedProvider([first, second, response()])
        with TemporaryDirectory() as directory:
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(
                    Path(directory), tools=tools,
                    policy=AllowListPolicy({"capabilities_search", "extra_7"}),
                ) as active:
                    result = await active.run("use extra 7").result()
                    self.assertEqual(result.tool_calls, 2)
                    self.assertEqual(active.session.messages[-2].text, "7")

    async def test_tool_schema_and_execution_bindings_change_together_at_request_boundary(self):
        entered, release = asyncio.Event(), asyncio.Event()
        executed = []
        @tool(name="calculate", source="mcp/demo", capability_type="mcp")
        def old_calculate(value: int) -> str:
            executed.append(("old", value))
            return "old result"
        @tool(name="calculate", source="mcp/demo", capability_type="mcp")
        def new_calculate(value: str, suffix: str) -> str:
            executed.append(("new", value, suffix))
            return "new result"
        current = [old_calculate]
        managers = []
        class Manager:
            def __init__(self, workspace):
                self.tools = ()
                self.statuses = ()
                self.dirty = False
                self.closed = False
                managers.append(self)
            async def reload(self):
                self.tools = tuple(current)
                return self.tools
            async def aclose(self):
                self.closed = True
        class Allow:
            def decide(self, context):
                return PolicyDecision.allow()
        async def first(messages, tools):
            definition = next(item for item in tools if item.name == "calculate")
            self.assertEqual(definition.input_schema["properties"]["value"]["type"], "integer")
            entered.set()
            await release.wait()
            return response(calls=[ToolUseContent("old-call", "calculate", {"value": 7})])
        async def second(messages, tools):
            definition = next(item for item in tools if item.name == "calculate")
            self.assertEqual(definition.input_schema["properties"]["value"]["type"], "string")
            self.assertEqual(set(definition.input_schema["required"]), {"value", "suffix"})
            self.assertEqual(messages[-1].text, "old result")
            return response(calls=[ToolUseContent(
                "new-call", "calculate", {"value": "seven", "suffix": "!"},
            )])
        provider = ScriptedProvider([first, second, response()])
        with TemporaryDirectory() as directory:
            with patch("sec.apis.client.create_provider", return_value=provider), patch(
                "sec.integrations.mcp.MCPManager", Manager,
            ):
                async with client(Path(directory), policy=Allow()) as active:
                    run = active.run("calculate")
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    current[:] = [new_calculate]
                    report = await active.reload_resources()
                    self.assertIn("tool/calculate", report.updated)
                    self.assertFalse(managers[0].closed)
                    release.set()
                    await run.result()
                    self.assertEqual(executed, [("old", 7), ("new", "seven", "!")])
                    self.assertTrue(managers[0].closed)

    async def test_normal_request_sees_skills_and_native_loading(self):
        provider = ScriptedProvider([
            response(calls=[ToolUseContent("load", "skills_load", {"name": "inspect"})]),
            response(),
        ])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            skill(root)
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root, policy=PermissionModePolicy("auto", workspace_root=root)) as active:
                    result = await active.run("Inspect this project").result()
                    self.assertEqual(result.tool_calls, 1)
                    first = provider.requests[0]
                    self.assertIn("inspect [auto]", first[0][0].text)
                    self.assertIn("skills_load", {item.name for item in first[1]})
                    self.assertIn("Inspect carefully.", provider.requests[1][0][0].text)

    async def test_required_skill_is_loaded_without_model_cooperation(self):
        provider = ScriptedProvider([response()])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            skill(root, activation="required")
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root) as active:
                    await active.run("Please inspect").result()
            self.assertIn("Inspect carefully.", provider.requests[0][0][0].text)

    async def test_reload_during_request_preserves_old_binding_then_uses_new_catalog(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def gated(messages, tools):
            entered.set()
            await release.wait()
            return response(calls=[ToolUseContent("old", "skills_load", {"name": "inspect"})])
        provider = ScriptedProvider([gated, response()])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = skill(root, content="Version one")
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root, policy=PermissionModePolicy("auto", workspace_root=root)) as active:
                    session_id = active.session.id
                    run = active.run("Inspect")
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    path.write_text(
                        "---\nname: inspect\ndescription: Inspect\n---\nVersion two",
                        encoding="utf-8",
                    )
                    report = await active.reload_resources()
                    self.assertTrue(report.pending)
                    release.set()
                    await run.result()
                    self.assertEqual(active.session.id, session_id)
                    results = [m for m in active.session.messages if m.role == "tool_result"]
                    self.assertEqual(results[0].text, "Version one")
                    self.assertIn("Version two", provider.requests[1][0][0].text)
                    self.assertNotEqual(
                        provider.requests[0][0][0].text, provider.requests[1][0][0].text,
                    )

    async def test_failed_reload_keeps_generation_and_old_skill(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = skill(root)
            provider = ScriptedProvider([response()])
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root) as active:
                    before = active._current_capabilities()
                    path.write_text("---\ninvalid\n---\nbroken", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        await active.reload_resources()
                    self.assertIs(active._current_capabilities(), before)
                    await active.run("hello").result()
                    self.assertIn("inspect [auto]", provider.requests[0][0][0].text)

    async def test_agent_switch_queues_and_persists_on_same_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def gated(messages, tools):
            entered.set()
            await release.wait()
            return response()
        provider = ScriptedProvider([gated, response()])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            card(root, "writer")
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root) as active:
                    session_id = active.session.id
                    run = active.run("hello")
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    await active.select_agent("writer")
                    self.assertEqual(active.pending_agent_id, "writer")
                    self.assertEqual(active.selected_agent_id, "default")
                    release.set()
                    await run.result()
                    await active.run("continue").result()
                    self.assertEqual(active.selected_agent_id, "writer")
                    self.assertEqual(active.session.id, session_id)
                    self.assertIn("Act as writer.", provider.requests[1][0][0].text)
                    store = active._store
                async with client(root, store=store, session_id=session_id) as restored:
                    self.assertEqual(restored.selected_agent_id, "writer")

    async def test_agent_choice_follows_fork_and_switch_and_missing_card_fails_explicitly(self):
        provider = ScriptedProvider([response()])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = card(root, "writer")
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root) as active:
                    await active.select_agent("writer")
                    await active.run("write").result()
                    original = active.session.id
                    forked = await active.fork_session()
                    self.assertNotEqual(forked.id, original)
                    self.assertEqual(active.selected_agent_id, "writer")
                    await active.select_agent("default")
                    await active.switch_session(original)
                    self.assertEqual(active.selected_agent_id, "writer")
                    writer_copy = await active.clone_session()
                    await active.switch_session(original)
                    path.unlink()
                    before = active._current_capabilities()
                    with self.assertRaises(ValueError):
                        await active.reload_resources()
                    self.assertIs(active._current_capabilities(), before)
                    await active.select_agent("default")
                    await active.reload_resources()
                    current = active.session.id
                    with self.assertRaises(ValueError):
                        await active.switch_session(writer_copy.id)
                    self.assertEqual(active.session.id, current)

    async def test_revocation_while_model_request_in_flight_denies_execution(self):
        entered, release = asyncio.Event(), asyncio.Event()
        executed = []
        @tool
        def dangerous() -> str:
            executed.append(True)
            return "should not execute"
        async def gated(messages, tools):
            entered.set()
            await release.wait()
            return response(calls=[ToolUseContent("call", "dangerous", {})])
        provider = ScriptedProvider([gated, response()])
        with TemporaryDirectory() as directory:
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(Path(directory), tools=(dangerous,),
                                  policy=AllowListPolicy({"dangerous"})) as active:
                    run = active.run("go")
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    await active.revoke_capability("dangerous")
                    release.set()
                    await run.result()
                    self.assertFalse(executed)
                    self.assertIn("revoked", active.session.messages[-2].text)

    async def test_delegation_is_independent_and_accounted(self):
        provider = ScriptedProvider([
            response(calls=[ToolUseContent("delegate", "agents_delegate",
                                          {"agent_id": "writer", "task": "Write a sentence"})]),
            response("child answer"),
            response("parent answer"),
        ])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            card(root, "coordinator",
                 '[delegation]\nallowed_agents = ["writer"]\nmax_depth = 1\n')
            card(root, "writer")
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root, agent_id="coordinator",
                                  policy=AllowListPolicy({"agents_delegate"})) as active:
                    run = active.run("write")
                    events = []
                    async def consume():
                        async for event in run.events():
                            events.append(event)
                    consumer = asyncio.create_task(consume())
                    result = await run.result()
                    await consumer
                    self.assertEqual(result.model_calls, 3)
                    self.assertEqual(result.usage.input_tokens, 3)
                    self.assertFalse(any(m.text == "Write a sentence" for m in active.session.messages))
                    delegated = [e for e in events if e.type == "agent_delegation"]
                    self.assertEqual([e.status for e in delegated], ["started", "completed"])
                    self.assertNotEqual(delegated[0].child_session_id, active.session.id)

    async def test_child_cannot_expand_parent_tool_scope(self):
        called = []
        @tool
        def restricted() -> str:
            called.append(True)
            return "not allowed"
        async def child_response(messages, definitions):
            self.assertNotIn("restricted", {item.name for item in definitions})
            return response(calls=[ToolUseContent("attempt", "restricted", {})])
        provider = ScriptedProvider([
            response(calls=[ToolUseContent("delegate", "agents_delegate",
                                          {"agent_id": "writer", "task": "attempt"})]),
            child_response, response("child done"), response("parent done"),
        ])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            card(root, "coordinator",
                 '[delegation]\nallowed_agents = ["writer"]\nmax_depth = 1\n')
            card(root, "writer", '[capabilities]\ntools = ["restricted"]\n')
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(
                    root, agent_id="coordinator", tools=(restricted,),
                    policy=AllowListPolicy({"agents_delegate", "restricted"}),
                ) as active:
                    await active.run("delegate").result()
                    self.assertFalse(called)

    async def test_agent_tree_cannot_bypass_root_budget(self):
        provider = ScriptedProvider([
            response(calls=[ToolUseContent("delegate", "agents_delegate",
                                          {"agent_id": "writer", "task": "Write"})]),
            response("child answer"),
        ])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            card(root, "coordinator",
                 '[delegation]\nallowed_agents = ["writer"]\nmax_depth = 1\n')
            card(root, "writer")
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root, agent_id="coordinator", max_model_calls=2,
                                  policy=AllowListPolicy({"agents_delegate"})) as active:
                    with self.assertRaises(RunLimitExceeded):
                        await active.run("write").result()
                    self.assertEqual(len(provider.requests), 2)

    async def test_revocation_is_rechecked_after_user_approval(self):
        executed = []
        @tool
        def operation() -> str:
            executed.append(True)
            return "executed"
        provider = ScriptedProvider([
            response(calls=[ToolUseContent("op", "operation", {})]), response(),
        ])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(
                    root, tools=(operation,),
                    policy=PermissionModePolicy("normal", workspace_root=root),
                ) as active:
                    run = active.run("operate")
                    async for event in run.events():
                        if event.type == "tool_approval_requested":
                            await active.revoke_capability("operation")
                            self.assertTrue(run.approve_tool(event.tool_call_id))
                    await run.result()
                    self.assertFalse(executed)

    async def test_child_approval_is_forwarded_to_root(self):
        executed = []
        @tool
        def operation() -> str:
            executed.append(True)
            return "executed"
        provider = ScriptedProvider([
            response(calls=[ToolUseContent("delegate", "agents_delegate",
                                          {"agent_id": "writer", "task": "operate"})]),
            response(calls=[ToolUseContent("op", "operation", {})]),
            response("child done"), response("parent done"),
        ])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            card(root, "coordinator",
                 '[capabilities]\ntools = ["operation"]\n'
                 '[delegation]\nallowed_agents = ["writer"]\nmax_depth = 1\n')
            card(root, "writer", '[capabilities]\ntools = ["operation"]\n')
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(
                    root, agent_id="coordinator", tools=(operation,),
                    policy=PermissionModePolicy("normal", workspace_root=root),
                ) as active:
                    run = active.run("delegate")
                    approvals = []
                    async for event in run.events():
                        if event.type == "tool_approval_requested":
                            approvals.append(event.tool_name)
                            self.assertTrue(run.approve_tool(event.tool_call_id))
                    await run.result()
                    self.assertEqual(approvals, ["agents_delegate", "operation"])
                    self.assertEqual(executed, [True])

    async def test_cancel_parent_stops_child_request(self):
        child_entered = asyncio.Event()
        child_cancelled = asyncio.Event()
        async def blocked(messages, tools):
            child_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_cancelled.set()
        provider = ScriptedProvider([
            response(calls=[ToolUseContent("delegate", "agents_delegate",
                                          {"agent_id": "writer", "task": "wait"})]),
            blocked,
        ])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            card(root, "coordinator",
                 '[delegation]\nallowed_agents = ["writer"]\nmax_depth = 1\n')
            card(root, "writer")
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with client(root, agent_id="coordinator",
                                  policy=AllowListPolicy({"agents_delegate"})) as active:
                    run = active.run("delegate")
                    await asyncio.wait_for(child_entered.wait(), timeout=5)
                    run.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(run.result(), timeout=5)
                    self.assertTrue(child_cancelled.is_set())
