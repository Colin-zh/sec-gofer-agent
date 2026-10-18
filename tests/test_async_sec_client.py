"""验证 AsyncSecClient 的配置优先级、对话入口和资源关闭语义。"""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from unittest.mock import patch

from sec._types.content import TextContent
from sec._types.messages import AssistantMessage, Message, Usage, UsageCost, UserMessage
from sec._types.session import MessageEntry, SessionSummary
from sec.apis import AsyncSecClient
from sec.config import ConfigError
from sec.storage import InMemorySessionStore
from sec.tools.tool import ToolDefinition


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
        metadata={"usage_reported": False},
    )


class CloseableProvider:
    """提供确定性响应并记录由 Client 发起的关闭操作。"""

    def __init__(self) -> None:
        self.closed = False
        self.requests: list[tuple[Message, ...]] = []
        self.response_count = 0

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AssistantMessage:
        self.requests.append(tuple(messages))
        self.response_count += 1
        return _assistant_message(f"Client response {self.response_count}.")

    async def aclose(self) -> None:
        self.closed = True


class AsyncSecClientTests(unittest.IsolatedAsyncioTestCase):
    """覆盖常规 Client 生命周期与显式参数覆盖行为。"""

    async def test_client_runs_a_conversation_and_closes_owned_provider(self) -> None:
        provider = CloseableProvider()
        with patch.dict(
            "os.environ",
            {
                "SEC_PROVIDER": "anthropic",
                "SEC_MODEL": "environment-model",
                "SEC_API_KEY": "environment-key",
                "SEC_DATABASE_URL": "sqlite+aiosqlite:///environment.db",
            },
            clear=True,
        ):
            with patch(
                "sec.apis.client.create_provider", return_value=provider
            ) as factory:
                async with AsyncSecClient(
                    "assistant",
                    "Follow the user.",
                    provider="openai",
                    model="explicit-model",
                    api_key="explicit-key",
                    max_model_calls=3,
                    session_store_backend="in_memory",
                    env_file=None,
                ) as client:
                    config = factory.call_args.args[0]
                    self.assertEqual(config.provider.name, "openai")
                    self.assertEqual(config.provider.model, "explicit-model")
                    self.assertEqual(config.provider.api_key, "explicit-key")
                    self.assertEqual(client.agent.max_model_calls, 3)
                    run = client.run("Hello")
                    result = await run.result()

                    self.assertEqual(result.final_message.text, "Client response 1.")
                    next_result = await client.run("Continue").result()
                    self.assertEqual(next_result.final_message.text, "Client response 2.")
                    self.assertEqual(len(provider.requests), 2)
                    self.assertTrue(
                        any(message.text == "Hello" for message in provider.requests[1])
                    )
                    self.assertTrue(
                        any(
                            message.text == "Client response 1."
                            for message in provider.requests[1]
                        )
                    )
                    self.assertEqual(client.session.messages[0].text, "Hello")
                self.assertTrue(provider.closed)

    async def test_context_compression_requires_an_injected_estimator(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OPENAI_MODEL": "environment-model",
                "OPENAI_API_KEY": "environment-key",
            },
            clear=True,
        ):
            with patch("sec.apis.client.create_provider") as factory:
                with self.assertRaisesRegex(
                    ConfigError,
                    "both context_window_tokens and a TokenEstimator",
                ):
                    async with AsyncSecClient(
                        "assistant",
                        "Follow the user.",
                        context_window_tokens=8192,
                        session_store_backend="in_memory",
                        env_file=None,
                    ):
                        pass
                factory.assert_not_called()

    async def test_session_switch_fork_clone_and_listing(self) -> None:
        provider = CloseableProvider()
        store = InMemorySessionStore()
        with patch.dict(
            "os.environ",
            {
                "OPENAI_MODEL": "test-model",
                "OPENAI_API_KEY": "test-key",
            },
            clear=True,
        ):
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with AsyncSecClient(
                    "assistant",
                    "Follow the user.",
                    store=store,
                    session_store_backend="in_memory",
                    env_file=None,
                ) as client:
                    original_id = client.session.id
                    root = MessageEntry(
                        "root",
                        None,
                        1.0,
                        UserMessage([TextContent("root")], 1.0),
                    )
                    first = MessageEntry(
                        "first",
                        "root",
                        2.0,
                        _assistant_message("first branch"),
                    )
                    second = MessageEntry(
                        "second",
                        "root",
                        3.0,
                        _assistant_message("active branch"),
                    )
                    await client.session.append_entry(root)
                    await client.session.append_entry(first)
                    await client.session.append_entry(second)

                    forked = await client.fork_session()
                    self.assertEqual(
                        [message.text for message in forked.messages],
                        ["root", "active branch"],
                    )
                    self.assertEqual(len(forked.entries), 2)

                    await client.switch_session(original_id)
                    cloned = await client.clone_session()
                    self.assertEqual(len(cloned.entries), 3)
                    self.assertEqual(
                        [message.text for message in cloned.messages],
                        ["root", "active branch"],
                    )
                    self.assertEqual(len(await client.list_sessions()), 3)

    async def test_session_switch_is_rejected_during_an_active_run(self) -> None:
        provider = CloseableProvider()
        with patch.dict(
            "os.environ",
            {
                "OPENAI_MODEL": "test-model",
                "OPENAI_API_KEY": "test-key",
            },
            clear=True,
        ):
            with patch("sec.apis.client.create_provider", return_value=provider):
                async with AsyncSecClient(
                    "assistant",
                    "Follow the user.",
                    session_store_backend="in_memory",
                    env_file=None,
                ) as client:
                    run = client.run("Hello")
                    with self.assertRaisesRegex(RuntimeError, "active Run"):
                        await client.switch_session()
                    with self.assertRaisesRegex(RuntimeError, "active Run"):
                        await client.rename_session(client.session.id, "Busy")
                    await run.result()

    async def test_rename_session_preserves_current_session_and_history(self) -> None:
        store = InMemorySessionStore()
        with patch("sec.apis.client.create_provider", return_value=CloseableProvider()):
            async with AsyncSecClient(
                "assistant", "Follow the user.", provider="openai",
                model="test", api_key="test", store=store,
                session_store_backend="in_memory", env_file=None,
            ) as client:
                await client.run("Hello").result()
                original = client.session
                history = original.messages
                await client.switch_session()
                current = client.session
                await client.rename_session(original.id, " Renamed ")
                self.assertIs(client.session, current)
                snapshot = await store.load(original.id)
                self.assertEqual(SessionSummary.from_snapshot(snapshot).name, "Renamed")
                self.assertEqual(snapshot.messages, history)
                await client.rename_session(current.id, "New session")
                self.assertEqual(
                    SessionSummary.from_snapshot(current.snapshot).name, "New session"
                )
                from sec.apps.cli import create_chat_commands

                outcome = await create_chat_commands(client).dispatch("/name CLI name")
                self.assertIn("CLI name", outcome.message)
                self.assertEqual(
                    SessionSummary.from_snapshot(current.snapshot).name, "CLI name"
                )
                with self.assertRaises(KeyError):
                    await client.rename_session("missing", "Name")
                with self.assertRaises(ValueError):
                    await client.rename_session(current.id, "  ")
                self.assertIs(client.session, current)


if __name__ == "__main__":
    unittest.main()
