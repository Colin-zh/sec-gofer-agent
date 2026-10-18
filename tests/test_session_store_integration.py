"""用确定性 Provider 和内存存储验证 Session/Runtime 持久化与恢复集成。"""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from sec._types.content import TextContent, ToolUseContent
from sec._types.messages import (
    AssistantMessage,
    Message,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from sec._types.session import MessageEntry, SessionEntry, SessionSnapshot
from sec.apis import (
    Config,
    ProviderSettings,
    StorageSettings,
    create_database,
    create_session_store,
)
from sec import (
    AllowListPolicy,
    InMemorySessionStore,
    SessionStoreError,
    SessionVersionConflictError,
    tool,
)
from sec.agent import Agent, Session
from sec._runtime.harness import Harness
from sec._types.storage import SessionStoreProtocol
from sec.tools.tool import ToolDefinition


def _assistant_message(
    content: list[TextContent | ToolUseContent],
) -> AssistantMessage:
    usage = Usage(0, 0, 0, 0, 0, UsageCost(0, 0, 0, 0, 0))
    return AssistantMessage(
        content,
        "test-api",
        "test-provider",
        "test-model",
        usage,
        "stop",
        1.0,
        metadata={"usage_reported": False},
    )


def _user_message(text: str) -> UserMessage:
    return UserMessage([TextContent(text)], 1.0)


class ScriptedProvider:
    """按预设顺序返回模型消息，并记录每次请求携带的完整历史。"""

    def __init__(self, *responses: Message) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[Message, ...]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> Message:
        self.requests.append(tuple(messages))
        if not self._responses:
            raise AssertionError("no scripted response remains")
        return self._responses.pop(0)


class FailOnNthAppend:
    """包装存储并在指定追加序号注入失败，不修改底层存储行为。"""

    def __init__(self, store: SessionStoreProtocol, fail_at: int) -> None:
        self._store = store
        self._fail_at = fail_at
        self.append_calls = 0

    async def load(self, session_id: str) -> SessionSnapshot:
        return await self._store.load(session_id)

    async def append(
        self,
        session_id: str,
        expected_version: int,
        entries: Sequence[SessionEntry],
        active_leaf_id: str,
    ) -> int:
        self.append_calls += 1
        if self.append_calls == self._fail_at:
            raise SessionStoreError("injected append failure")
        return await self._store.append(
            session_id,
            expected_version,
            entries,
            active_leaf_id,
        )


class SessionStoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """验证持久化提交、版本冲突和中断工具调用恢复语义。"""

    async def test_run_persists_each_message_before_exposing_it(self) -> None:
        store = InMemorySessionStore()
        session = Session(id="persisted", store=store)
        provider = ScriptedProvider(_assistant_message([TextContent("answer")]))

        result = await Harness(provider).run(
            Agent(name="assistant", instructions=""),
            "question",
            session=session,
        ).result()
        snapshot = await store.load("persisted")

        self.assertEqual(result.final_message.text, "answer")
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(snapshot.messages, session.messages)
        self.assertEqual([message.text for message in snapshot.messages], ["question", "answer"])

    async def test_stale_session_fails_before_requesting_a_model_response(self) -> None:
        store = InMemorySessionStore()
        entry = MessageEntry("written", None, 1.0, _user_message("written elsewhere"))
        await store.append("shared", 0, [entry], "written")
        stale_session = Session(id="shared", store=store)
        provider = ScriptedProvider()

        with self.assertRaises(SessionVersionConflictError):
            await Harness(provider).run(
                Agent(name="assistant", instructions=""),
                "another prompt",
                session=stale_session,
            ).result()

        self.assertEqual(provider.requests, [])
        self.assertEqual(stale_session.messages, ())

    async def test_restore_pairs_interrupted_tool_call_without_replaying_it(self) -> None:
        memory_store = InMemorySessionStore()
        store = FailOnNthAppend(memory_store, fail_at=3)
        side_effects: list[str] = []

        @tool
        def write_record() -> str:
            side_effects.append("written")
            return "record written"

        agent = Agent(
            name="writer",
            instructions="",
            tools=(write_record,),
        )
        first_provider = ScriptedProvider(
            _assistant_message(
                [ToolUseContent(id="write-1", name="write_record", arguments={})]
            )
        )
        interrupted_session = Session(id="interrupted", store=store)
        run = Harness(
            first_provider,
            policy=AllowListPolicy({"write_record"}),
        ).run(agent, "Write the record", session=interrupted_session)

        with self.assertRaisesRegex(SessionStoreError, "injected append failure"):
            await run.result()

        self.assertEqual(side_effects, ["written"])
        with self.assertRaisesRegex(SessionStoreError, "restore it before reuse"):
            Harness(ScriptedProvider()).run(
                agent,
                "Retry",
                session=interrupted_session,
            )

        restored = await Session.restore(store, "interrupted")
        recovery_message = restored.messages[-1]

        self.assertIsInstance(recovery_message, ToolResultMessage)
        self.assertTrue(recovery_message.is_error)
        self.assertIn("outcome is unknown", recovery_message.text)
        self.assertEqual(restored.version, 3)
        self.assertEqual(store.append_calls, 4)

        resumed_provider = ScriptedProvider(
            _assistant_message([TextContent("Continuing without replay.")])
        )
        result = await Harness(
            resumed_provider,
            policy=AllowListPolicy({"write_record"}),
        ).run(agent, "Continue", session=restored).result()

        self.assertEqual(result.final_message.text, "Continuing without replay.")
        self.assertEqual(side_effects, ["written"])
        prior_tool_use = resumed_provider.requests[0][2]
        paired_result_message = resumed_provider.requests[0][3]
        self.assertEqual(prior_tool_use.tool_uses[0].id, "write-1")
        self.assertEqual(
            paired_result_message.tool_call_id,
            prior_tool_use.tool_uses[0].id,
        )

    async def test_restore_recovers_tool_calls_only_on_the_active_branch(self) -> None:
        store = InMemorySessionStore()
        root = MessageEntry("root", None, 1.0, _user_message("root"))
        inactive_call = MessageEntry(
            "inactive-tool-call",
            "root",
            2.0,
            _assistant_message(
                [ToolUseContent(id="inactive-call", name="write_record", arguments={})]
            ),
        )
        active_answer = MessageEntry(
            "active-answer",
            "root",
            3.0,
            _assistant_message([TextContent("active branch")]),
        )
        await store.append(
            "branched-recovery",
            0,
            [root, inactive_call, active_answer],
            "active-answer",
        )

        restored = await Session.restore(store, "branched-recovery")

        self.assertEqual([message.text for message in restored.messages], ["root", "active branch"])
        self.assertEqual(restored.active_leaf_id, "active-answer")
        self.assertEqual(restored.version, 1)


class DatabaseSessionStoreIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """验证配置工厂、Harness 与 SQLAlchemy Store 的关闭重开恢复路径。"""

    async def test_history_survives_database_close_and_reopen(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_url = (
                f"sqlite+aiosqlite:///{Path(temporary_directory) / 'session.db'}"
            )
            config = _database_config(database_url)
            first_database = create_database(config)
            self.assertIsNotNone(first_database)
            try:
                first_store = create_session_store(config, database=first_database)
                await first_store.initialize()
                session = Session(id="durable", store=first_store)
                provider = ScriptedProvider(
                    _assistant_message([TextContent("persisted answer")])
                )
                result = await Harness(provider).run(
                    Agent(name="assistant", instructions=""),
                    "persist this",
                    session=session,
                ).result()
                self.assertEqual(result.final_message.text, "persisted answer")
            finally:
                await first_database.aclose()

            second_database = create_database(config)
            self.assertIsNotNone(second_database)
            try:
                second_store = create_session_store(config, database=second_database)
                await second_store.initialize()
                restored = await Session.restore(second_store, "durable")

                self.assertEqual(
                    [message.text for message in restored.messages],
                    ["persist this", "persisted answer"],
                )
                self.assertEqual(len(restored.entries), 2)
                self.assertEqual(restored.active_leaf_id, restored.entries[-1].id)
                self.assertEqual(restored.version, 2)
            finally:
                await second_database.aclose()

    async def test_unknown_tool_result_is_repaired_without_replay(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_url = (
                f"sqlite+aiosqlite:///{Path(temporary_directory) / 'recovery.db'}"
            )
            config = _database_config(database_url)
            first_database = create_database(config)
            self.assertIsNotNone(first_database)
            side_effects: list[str] = []

            @tool
            def write_record() -> str:
                side_effects.append("written")
                return "record written"

            agent = Agent(
                name="writer",
                instructions="",
                tools=(write_record,),
            )
            try:
                store = create_session_store(config, database=first_database)
                await store.initialize()
                failing_store = FailOnNthAppend(store, fail_at=3)
                provider = ScriptedProvider(
                    _assistant_message(
                        [ToolUseContent(id="sql-write-1", name="write_record", arguments={})]
                    )
                )
                run = Harness(
                    provider,
                    policy=AllowListPolicy({"write_record"}),
                ).run(
                    agent,
                    "Write the record",
                    session=Session(id="sql-interrupted", store=failing_store),
                )

                with self.assertRaisesRegex(SessionStoreError, "injected append failure"):
                    await run.result()
                self.assertEqual(side_effects, ["written"])
            finally:
                await first_database.aclose()

            second_database = create_database(config)
            self.assertIsNotNone(second_database)
            try:
                restored_store = create_session_store(config, database=second_database)
                await restored_store.initialize()
                restored = await Session.restore(
                    restored_store,
                    "sql-interrupted",
                )
                recovery_result = restored.messages[-1]
                self.assertIsInstance(recovery_result, ToolResultMessage)
                self.assertTrue(recovery_result.is_error)

                continuation_provider = ScriptedProvider(
                    _assistant_message([TextContent("continued")])
                )
                result = await Harness(
                    continuation_provider,
                    policy=AllowListPolicy({"write_record"}),
                ).run(agent, "Continue", session=restored).result()

                self.assertEqual(result.final_message.text, "continued")
                self.assertEqual(side_effects, ["written"])
            finally:
                await second_database.aclose()


def _database_config(database_url: str) -> Config:
    """创建指向测试 SQLite 文件的显式配置。"""
    return Config(
        provider=ProviderSettings(
            name="openai",
            model="test-model",
            api_key="test-secret",
        ),
        storage=StorageSettings(
            session_store_backend="database",
            database_url=database_url,
        ),
    )
