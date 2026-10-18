"""定义异步 SessionStore 契约测试，供各存储后端复用。"""

from __future__ import annotations

import asyncio
import unittest

from sec._types.content import TextContent, ThinkingContent
from sec._types.messages import AssistantMessage, SystemMessage, Usage, UsageCost, UserMessage
from sec._types.storage import SessionStoreProtocol
from sec.exceptions import SessionStoreError, SessionVersionConflictError
from sec.storage import (
    Database,
    DatabaseSessionStore,
    InMemorySessionStore,
    SessionStore,
)
from sec._types.session import (
    BranchSummaryEntry,
    CompactionEntry,
    ContextEditEntry,
    ContextEditReplacement,
    CustomEntry,
    CustomMessageEntry,
    LabelEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionInfoEntry,
    SessionSnapshot,
    SessionSummary,
    ThinkingLevelChangeEntry,
    UsageEntry,
)


def _user_message(text: str, **metadata: object) -> UserMessage:
    return UserMessage([TextContent(text)], 1.0, metadata)


def _assistant_message(text: str) -> AssistantMessage:
    usage = Usage(0, 0, 0, 0, 0, UsageCost(0, 0, 0, 0, 0))
    return AssistantMessage(
        [TextContent(text)],
        "test-api",
        "test-provider",
        "test-model",
        usage,
        "stop",
        1.0,
    )


def _entry(
    entry_id: str,
    message: UserMessage | AssistantMessage,
    parent_id: str | None = None,
) -> MessageEntry:
    return MessageEntry(entry_id, parent_id, message.timestamp, message)


class SessionStoreContract:
    """复用同一组异步契约检查不同 SessionStore 后端。"""

    async def make_store(self) -> SessionStoreProtocol:
        raise NotImplementedError

    async def test_missing_session_loads_as_empty_version_zero(self) -> None:
        store = await self.make_store()

        snapshot = await store.load("new-session")

        self.assertEqual(snapshot.session_id, "new-session")
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.entries, ())
        self.assertIsNone(snapshot.active_leaf_id)

    async def test_list_sessions_returns_recent_named_sessions(self) -> None:
        store = await self.make_store()
        root = _entry("root-a", _user_message("first"))
        await store.append("session-a", 0, [root], root.id)
        named = SessionInfoEntry("name-a", root.id, 2.0, "Session A")
        await store.append("session-a", 1, [named], named.id)
        recent = _entry("root-b", _user_message("second"))
        await store.append("session-b", 0, [recent], recent.id)

        summaries = await store.list_sessions()

        self.assertEqual(
            summaries,
            (
                SessionSummary("session-a", 2, "name-a", "Session A", 2.0),
                SessionSummary("session-b", 1, "root-b", None, 1.0),
            ),
        )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            await store.list_sessions(0)

    async def test_append_is_ordered_and_increments_version_once_per_batch(self) -> None:
        store = await self.make_store()
        entries = [
            _entry("question", _user_message("question")),
            _entry("answer", _assistant_message("answer"), "question"),
        ]

        new_version = await store.append("ordered", 0, entries, "answer")
        loaded = await store.load("ordered")

        self.assertEqual(new_version, 1)
        self.assertEqual(loaded.version, 1)
        self.assertEqual(loaded.entries, tuple(entries))
        self.assertEqual(loaded.active_leaf_id, "answer")
        self.assertEqual([message.text for message in loaded.messages], ["question", "answer"])

    async def test_snapshot_projects_only_the_selected_tree_branch(self) -> None:
        store = await self.make_store()
        root = _entry("root", _user_message("root"))
        first_branch = _entry("first-branch", _assistant_message("first"), "root")
        await store.append("branched", 0, [root, first_branch], "first-branch")
        second_branch = _entry("second-branch", _assistant_message("second"), "root")

        await store.append("branched", 1, [second_branch], "second-branch")
        snapshot = await store.load("branched")

        self.assertEqual(
            [message.text for message in snapshot.messages],
            ["root", "second"],
        )
        self.assertEqual(
            {entry.id for entry in snapshot.entries},
            {"root", "first-branch", "second-branch"},
        )
        self.assertEqual(snapshot.active_leaf_id, "second-branch")

    async def test_every_history_variant_round_trips_through_store(self) -> None:
        store = await self.make_store()
        usage = Usage(
            10,
            4,
            2,
            1,
            17,
            UsageCost(0.1, 0.2, 0.03, 0.04, 0.37),
        )
        message = _user_message("root")
        entries = [
            MessageEntry("message", None, 1.0, message),
            ThinkingLevelChangeEntry("thinking", "message", 2.0, "high"),
            ModelChangeEntry("model", "thinking", 3.0, "provider", "model-id"),
            UsageEntry(
                "usage",
                "model",
                4.0,
                "completion",
                "provider",
                "model-id",
                usage,
                "ok",
            ),
            CompactionEntry(
                "compaction",
                "usage",
                5.0,
                "summary",
                "message",
                22,
                {"tokens_after": 8},
                usage,
                False,
                SystemMessage(content="system", timestamp=5.0),
            ),
            BranchSummaryEntry(
                "branch",
                "compaction",
                6.0,
                "message",
                "branch summary",
                {"branch": 1},
                usage,
                True,
            ),
            CustomEntry("custom", "branch", 7.0, "plugin", {"value": "data"}),
            CustomMessageEntry(
                "custom-message",
                "custom",
                8.0,
                "plugin",
                [TextContent("shown")],
                True,
            ),
            ContextEditEntry(
                "context-edit",
                "custom-message",
                9.0,
                "message",
                ContextEditReplacement([ThinkingContent("replacement")]),
            ),
            LabelEntry("label", "context-edit", 10.0, "message", "bookmark"),
            SessionInfoEntry("session-info", "label", 11.0, "renamed"),
        ]

        await store.append("all-entry-types", 0, entries, "session-info")
        snapshot = await store.load("all-entry-types")

        self.assertEqual(snapshot.entries, tuple(entries))
        self.assertEqual(snapshot.active_leaf_id, "session-info")
        self.assertEqual(snapshot.messages, (message,))

    async def test_stale_version_conflicts_without_mutating_history(self) -> None:
        store = await self.make_store()
        first = _entry("first", _user_message("first"))
        await store.append("conflicted", 0, [first], "first")

        with self.assertRaises(SessionVersionConflictError) as raised:
            stale = _entry("stale", _user_message("stale"))
            await store.append("conflicted", 0, [stale], "stale")

        self.assertEqual(raised.exception.session_id, "conflicted")
        self.assertEqual(raised.exception.expected_version, 0)
        self.assertEqual(raised.exception.actual_version, 1)
        snapshot = await store.load("conflicted")
        self.assertEqual(snapshot.version, 1)
        self.assertEqual([message.text for message in snapshot.messages], ["first"])

    async def test_concurrent_compare_and_append_allows_only_one_writer(self) -> None:
        store = await self.make_store()
        outcomes = await asyncio.gather(
            store.append("racing", 0, [_entry("left", _user_message("left"))], "left"),
            store.append("racing", 0, [_entry("right", _user_message("right"))], "right"),
            return_exceptions=True,
        )

        self.assertEqual(sum(type(item) is int for item in outcomes), 1)
        conflicts = [
            item for item in outcomes if isinstance(item, SessionVersionConflictError)
        ]
        self.assertEqual(len(conflicts), 1)
        snapshot = await store.load("racing")
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(len(snapshot.messages), 1)

    async def test_append_failure_does_not_leave_a_partial_batch(self) -> None:
        store = await self.make_store()

        class Uncopyable:
            """通过复制失败模拟存储批次准备阶段的故障。"""

            def __deepcopy__(self, memo: dict[int, object]) -> Uncopyable:
                raise RuntimeError("copy failed")

        broken_message = _user_message("first", payload=Uncopyable())
        broken_entry = _entry("first", broken_message)

        with self.assertRaises(SessionStoreError):
            await store.append(
                "failed",
                0,
                [broken_entry],
                "first",
            )

        snapshot = await store.load("failed")
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.entries, ())

    async def test_empty_batch_is_rejected_without_incrementing_version(self) -> None:
        store = await self.make_store()

        with self.assertRaisesRegex(ValueError, "at least one entry"):
            await store.append("empty", 0, [], "missing")

        snapshot = await store.load("empty")
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.messages, ())

    async def test_loaded_snapshot_does_not_alias_stored_messages(self) -> None:
        store = await self.make_store()
        original = _entry("original", _user_message("original"))
        await store.append("detached", 0, [original], "original")
        original.message.content[0].text = "mutated input"

        snapshot = await store.load("detached")
        snapshot.messages[0].content[0].text = "mutated loaded copy"
        reloaded = await store.load("detached")

        self.assertEqual(reloaded.messages[0].text, "original")

    async def test_invalid_session_id_and_version_are_rejected(self) -> None:
        store = await self.make_store()

        with self.assertRaisesRegex(ValueError, "session ID"):
            await store.load("  ")
        with self.assertRaisesRegex(ValueError, "expected version"):
            await store.append(
                "invalid",
                -1,
                [_entry("message", _user_message("message"))],
                "message",
            )


class InMemorySessionStoreContractTests(
    SessionStoreContract,
    unittest.IsolatedAsyncioTestCase,
):
    """在 InMemorySessionStore 上运行完整存储契约测试。"""

    async def make_store(self) -> SessionStoreProtocol:
        return InMemorySessionStore()


class DatabaseSessionStoreContractTests(
    SessionStoreContract,
    unittest.IsolatedAsyncioTestCase,
):
    """在 SQLAlchemy 后端运行完整异步 SessionStore 契约测试。"""

    async def asyncSetUp(self) -> None:
        self.database = Database("sqlite+aiosqlite:///:memory:")
        self.store = DatabaseSessionStore(self.database)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.database.aclose()

    async def make_store(self) -> SessionStoreProtocol:
        return self.store


class SessionStoreAbstractMethodTests(unittest.TestCase):
    """验证显式继承 SessionStore 的实现必须落实全部抽象操作。"""

    def test_incomplete_implementation_cannot_be_instantiated(self) -> None:
        class IncompleteSessionStore(SessionStoreProtocol):
            """故意缺少协议方法，用于验证运行时抽象约束。"""

            pass

        with self.assertRaises(TypeError):
            IncompleteSessionStore()


class UnifiedSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    """验证公开 SessionStore 类按配置委托给对应 adapter。"""

    async def test_in_memory_backend_is_the_default(self) -> None:
        store = SessionStore()
        await store.initialize()

        entry = _entry("hello", _user_message("hello"))
        version = await store.append("facade-memory", 0, [entry], "hello")
        snapshot = await store.load("facade-memory")

        self.assertEqual(version, 1)
        self.assertEqual(snapshot.messages[0].text, "hello")

    async def test_database_backend_uses_injected_database(self) -> None:
        database = Database("sqlite+aiosqlite:///:memory:")
        try:
            store = SessionStore(backend="database", database=database)
            await store.initialize()

            version = await store.append(
                "facade-database",
                0,
                [_entry("persisted", _user_message("persisted"))],
                "persisted",
            )
            snapshot = await store.load("facade-database")

            self.assertEqual(version, 1)
            self.assertEqual(snapshot.messages[0].text, "persisted")
        finally:
            await database.aclose()

    def test_invalid_backend_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            SessionStore(backend="unknown")  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "requires a Database"):
            SessionStore(backend="database")
        with self.assertRaisesRegex(ValueError, "only valid"):
            SessionStore(database=object())  # type: ignore[arg-type]
