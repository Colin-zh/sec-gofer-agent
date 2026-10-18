from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError

from sec import AsyncSecClient
from sec._types.session import SessionInfoEntry
from sec.exceptions import SessionStoreError, SessionVersionConflictError
from sec.storage import Database, DatabaseSessionStore, InMemorySessionStore, SessionStore
from sec.storage.session_store.database.schema import entries, sessions


class SessionDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_and_database_delete_compare_versions_and_isolate_sessions(self) -> None:
        database = Database("sqlite+aiosqlite:///:memory:")
        try:
            await database.run_sync(
                lambda connection: connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            )
            db_store = DatabaseSessionStore(database)
            await db_store.initialize()
            for store in (InMemorySessionStore(), db_store, SessionStore()):
                with self.subTest(store=type(store).__name__):
                    await store.append("target", 0, (SessionInfoEntry("a", None, 1, "A"),), "a")
                    await store.append("target", 1, (SessionInfoEntry("b", "a", 2, "B"),), "b")
                    await store.append("keep", 0, (SessionInfoEntry("c", None, 1, "C"),), "c")
                    with self.assertRaises(SessionVersionConflictError):
                        await store.delete("target", 1)
                    self.assertEqual((await store.load("target")).version, 2)
                    await store.delete("target", 2)
                    self.assertEqual((await store.load("target")).entries, ())
                    self.assertEqual((await store.load("keep")).version, 1)
                    self.assertEqual([s.session_id for s in await store.list_sessions()], ["keep"])
                    await store.delete("target", 0)
                    with self.assertRaises(ValueError):
                        await store.delete("keep", -1)
            async with database.session() as connection:
                self.assertEqual(await connection.scalar(
                    select(func.count()).select_from(entries).where(entries.c.session_id == "target")
                ), 0)
                self.assertEqual(await connection.scalar(
                    select(func.count()).select_from(sessions).where(sessions.c.session_id == "target")
                ), 0)
        finally:
            await database.aclose()

    async def test_database_delete_rolls_back_entries_and_version_on_failure(self) -> None:
        database = Database("sqlite+aiosqlite:///:memory:")
        try:
            store = DatabaseSessionStore(database)
            await store.initialize()
            await store.append("target", 0, (SessionInfoEntry("a", None, 1, "A"),), "a")
            before = await store.load("target")

            def fail_delete(connection, cursor, statement, parameters, context, many):
                if statement.startswith("DELETE FROM sec_sessions"):
                    raise SQLAlchemyError("injected failure after deleting entries")

            event.listen(database._engine.sync_engine, "before_cursor_execute", fail_delete)
            try:
                with self.assertRaisesRegex(SessionStoreError, "could not delete"):
                    await store.delete("target", 1)
            finally:
                event.remove(database._engine.sync_engine, "before_cursor_execute", fail_delete)
            self.assertEqual(await store.load("target"), before)
        finally:
            await database.aclose()

    async def test_client_selected_fork_clone_export_and_delete_do_not_switch_source(self) -> None:
        from test_async_sec_client import CloseableProvider

        store = InMemorySessionStore()
        with patch("sec.apis.client.create_provider", return_value=CloseableProvider()):
            async with AsyncSecClient(
                "desktop", "", provider="openai", model="test", api_key="test",
                env_file=None, store=store, session_store_backend="in_memory",
            ) as client:
                await client.run("Hello").result()
                source = client.session.id
                source_snapshot = client.session.snapshot
                await client.switch_session()
                current = client.session
                self.assertEqual(await client.get_session_snapshot(source), source_snapshot)
                self.assertIs(client.session, current)
                fork = await client.fork_session(source)
                self.assertEqual([m.text for m in fork.messages], [m.text for m in source_snapshot.messages])
                clone = await client.clone_session(source)
                self.assertNotEqual(clone.id, source)
                run = client.run("Busy")
                with self.assertRaisesRegex(RuntimeError, "active Run"):
                    await client.delete_session(source)
                await run.result()
                await client.delete_session(source)
                self.assertIs(client.session, clone)
                self.assertEqual((await store.load(source)).entries, ())
                await client.delete_session(clone.id)
                self.assertNotEqual(client.session.id, clone.id)
                self.assertEqual(client.session.messages, ())
                with self.assertRaises(KeyError):
                    await client.delete_session("missing")
