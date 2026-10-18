"""验证数据库 SessionStore 序列化、批次回滚及独立资源并发 CAS。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from sec._types.content import TextContent
from sec._types.messages import UserMessage
from sec._types.session import MessageEntry
from sec.exceptions import SessionStoreError, SessionVersionConflictError
from sec.storage import Database, DatabaseSessionStore
from sec.storage.session_store.codec import encode_session_entry
from sec.storage.session_store.database.schema import entries


def _user_message(text: str, **metadata: object) -> UserMessage:
    return UserMessage(
        content=[TextContent(text=text)],
        timestamp=1.0,
        metadata=metadata,
    )


def _entry(entry_id: str, text: str) -> MessageEntry:
    message = _user_message(text)
    return MessageEntry(entry_id, None, message.timestamp, message)


def _create_legacy_schema(connection: Connection) -> None:
    """创建早期 SessionStore schema，供升级测试使用。"""
    connection.exec_driver_sql(
        "CREATE TABLE sec_sessions ("
        "session_id VARCHAR(255) NOT NULL PRIMARY KEY, "
        "version INTEGER NOT NULL)"
    )
    connection.exec_driver_sql(
        "CREATE TABLE sec_session_entries ("
        "session_id VARCHAR(255) NOT NULL, "
        "sequence INTEGER NOT NULL, "
        "entry_id VARCHAR(255) NOT NULL, "
        "parent_id VARCHAR(255), "
        "payload TEXT NOT NULL, "
        "PRIMARY KEY (session_id, entry_id))"
    )
    root = _entry("legacy-root", "root")
    leaf = MessageEntry(
        "legacy-leaf",
        root.id,
        2.0,
        _user_message("leaf"),
    )
    connection.execute(
        text(
            "INSERT INTO sec_sessions (session_id, version) "
            "VALUES ('legacy', 2)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO sec_session_entries "
            "(session_id, sequence, entry_id, parent_id, payload) "
            "VALUES (:session_id, :sequence, :entry_id, :parent_id, :payload)"
        ),
        [
            {
                "session_id": "legacy",
                "sequence": sequence,
                "entry_id": entry.id,
                "parent_id": entry.parent_id,
                "payload": encode_session_entry(entry),
            }
            for sequence, entry in enumerate((root, leaf))
        ],
    )


class DatabaseSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    """覆盖序列化失败、部分批次回滚、损坏载荷及独立数据库资源并发写入。"""

    async def asyncSetUp(self) -> None:
        self.database = Database("sqlite+aiosqlite:///:memory:")
        self.store = DatabaseSessionStore(self.database)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.database.aclose()

    async def test_non_json_message_fails_without_mutating_store(self) -> None:
        message = _user_message("hello", unsupported=object())

        with self.assertRaisesRegex(SessionStoreError, "cannot be serialized"):
            await self.store.append(
                "non-json",
                0,
                [MessageEntry("non-json-entry", None, 1.0, message)],
                "non-json-entry",
            )

        snapshot = await self.store.load("non-json")
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.entries, ())

    async def test_corrupt_payload_fails_explicitly(self) -> None:
        await self.store.append(
            "corrupt", 0, [_entry("corrupt-entry", "hello")], "corrupt-entry"
        )
        async with self.database.transaction() as session:
            await session.execute(
                update(entries)
                .where(entries.c.session_id == "corrupt")
                .values(payload="{")
            )

        with self.assertRaisesRegex(SessionStoreError, "not valid JSON"):
            await self.store.load("corrupt")

    async def test_initialize_includes_database_error_details(self) -> None:
        store = DatabaseSessionStore(self.database)
        with patch.object(
            self.database,
            "run_sync",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("injected schema error"),
        ):
            with self.assertRaisesRegex(
                SessionStoreError,
                "SQLAlchemyError: injected schema error",
            ):
                await store.initialize()

    async def test_initialize_upgrades_legacy_active_leaf_schema(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_url = (
                f"sqlite+aiosqlite:///{Path(temporary_directory) / 'legacy.db'}"
            )
            database = Database(database_url)
            try:
                await database.run_sync(_create_legacy_schema)
                store = DatabaseSessionStore(database)
                await store.initialize()

                snapshot = await store.load("legacy")
                self.assertEqual(snapshot.version, 2)
                self.assertEqual(snapshot.active_leaf_id, "legacy-leaf")
                self.assertEqual(
                    [message.text for message in snapshot.messages],
                    ["root", "leaf"],
                )

                next_entry = MessageEntry(
                    "next-entry",
                    "legacy-leaf",
                    3.0,
                    _user_message("next"),
                )
                version = await store.append(
                    "legacy",
                    2,
                    [next_entry],
                    "next-entry",
                )
                self.assertEqual(version, 3)
                self.assertEqual(
                    (await store.load("legacy")).active_leaf_id,
                    "next-entry",
                )
            finally:
                await database.aclose()

    async def test_failed_partial_batch_rolls_back_as_storage_error(self) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                text(
                    "CREATE TRIGGER reject_second_entry "
                    "BEFORE INSERT ON sec_session_entries "
                    "WHEN NEW.sequence = 1 "
                    "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
                )
            )

        with self.assertRaises(SessionStoreError) as raised:
            await self.store.append(
                "partial",
                0,
                [_entry("first", "first"), _entry("second", "second")],
                "second",
            )

        self.assertNotIsInstance(raised.exception, SessionVersionConflictError)
        snapshot = await self.store.load("partial")
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.entries, ())

    async def test_concurrent_database_instances_compare_and_append(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_url = (
                f"sqlite+aiosqlite:///{Path(temporary_directory) / 'store.db'}"
            )
            first_database = Database(database_url)
            second_database = Database(database_url)
            try:
                first_store = DatabaseSessionStore(first_database)
                second_store = DatabaseSessionStore(second_database)
                await first_store.initialize()
                await second_store.initialize()
                outcomes = await asyncio.gather(
                    first_store.append(
                        "shared", 0, [_entry("left", "left")], "left"
                    ),
                    second_store.append(
                        "shared", 0, [_entry("right", "right")], "right"
                    ),
                    return_exceptions=True,
                )

                self.assertEqual(sum(type(outcome) is int for outcome in outcomes), 1)
                conflicts = [
                    outcome
                    for outcome in outcomes
                    if isinstance(outcome, SessionVersionConflictError)
                ]
                self.assertEqual(len(conflicts), 1, repr(outcomes))
                snapshot = await first_store.load("shared")
                self.assertEqual(snapshot.version, 1)
                self.assertEqual(len(snapshot.messages), 1)
            finally:
                await first_database.aclose()
                await second_database.aclose()


if __name__ == "__main__":
    unittest.main()
