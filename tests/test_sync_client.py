"""验证同步 Client 的多轮对话、流式读取和取消清理。"""

from __future__ import annotations

import asyncio
import threading
import unittest
from collections.abc import AsyncIterator, Sequence
from unittest.mock import patch

from sec._types.content import TextContent
from sec._types.events import ContentDeltaEvent, MessageUpdateEvent, SessionEvent
from sec._types.messages import AssistantMessage, Message, Usage, UsageCost
from sec.apis import SecClient
from sec.tools.tool import ToolDefinition
from sec._types.streaming import ModelResponseCompleted, ModelTextDelta


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


class ScriptedProvider:
    def __init__(self) -> None:
        self.closed = False
        self.requests: list[tuple[Message, ...]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AssistantMessage:
        self.requests.append(tuple(messages))
        return _assistant_message(f"response-{len(self.requests)}")

    async def aclose(self) -> None:
        self.closed = True


class StreamingProvider:
    def __init__(self) -> None:
        self.closed = False
        self.cancelled = threading.Event()
        self.requests: list[tuple[Message, ...]] = []

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelTextDelta | ModelResponseCompleted]:
        async def generate() -> AsyncIterator[ModelTextDelta | ModelResponseCompleted]:
            self.requests.append(tuple(messages))
            yield ModelTextDelta("streamed ")
            yield ModelTextDelta("response")
            yield ModelResponseCompleted(
                _assistant_message("streamed response")
            )

        return generate()

    async def aclose(self) -> None:
        self.closed = True


class FailingProvider(ScriptedProvider):
    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AssistantMessage:
        raise RuntimeError("provider failure")


def _event_text_delta(event: SessionEvent) -> str | None:
    if not isinstance(event, MessageUpdateEvent):
        return None
    assistant_event = event.assistant_message_event
    if not isinstance(assistant_event, ContentDeltaEvent):
        return None
    if assistant_event.type != "text_delta":
        return None
    return assistant_event.delta


class BlockingStreamingProvider(StreamingProvider):
    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ModelTextDelta | ModelResponseCompleted]:
        async def generate() -> AsyncIterator[ModelTextDelta | ModelResponseCompleted]:
            try:
                yield ModelTextDelta("partial")
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

        return generate()


class SecClientTests(unittest.TestCase):
    def test_sync_client_runs_multiple_turns_in_one_session(self) -> None:
        provider = ScriptedProvider()
        with patch("sec.apis.client.create_provider", return_value=provider):
            with SecClient(
                "assistant",
                "Follow the user.",
                provider="openai",
                model="test-model",
                api_key="test-key",
                session_store_backend="in_memory",
                env_file=None,
            ) as client:
                first = client.run("Hello")
                second = client.run("Continue")

                self.assertEqual(first.text, "response-1")
                self.assertEqual(first.stop_reason, "stop")
                self.assertEqual(second.text, "response-2")
                self.assertEqual(len(provider.requests), 2)
                self.assertTrue(
                    any(message.text == "Hello" for message in provider.requests[1])
                )
                self.assertTrue(
                    any(
                        message.text == "response-1"
                        for message in provider.requests[1]
                    )
                )
            self.assertTrue(provider.closed)

    def test_sync_client_propagates_run_errors_and_closes_provider(self) -> None:
        provider = FailingProvider()
        with patch("sec.apis.client.create_provider", return_value=provider):
            with SecClient(
                "assistant",
                "Follow the user.",
                provider="openai",
                model="test-model",
                api_key="test-key",
                session_store_backend="in_memory",
                env_file=None,
            ) as client:
                with self.assertRaisesRegex(RuntimeError, "provider failure"):
                    client.run("Hello")

            self.assertTrue(provider.closed)

    def test_sync_stream_exposes_text_deltas_and_final_result(self) -> None:
        provider = StreamingProvider()
        with patch("sec.apis.client.create_provider", return_value=provider):
            with SecClient(
                "assistant",
                "Follow the user.",
                provider="openai",
                model="test-model",
                api_key="test-key",
                session_store_backend="in_memory",
                env_file=None,
            ) as client:
                with client.stream("Hello") as stream:
                    deltas = [
                        text_delta
                        for event in stream
                        if (text_delta := _event_text_delta(event)) is not None
                    ]
                    result = stream.result()

                self.assertEqual(deltas, ["streamed ", "response"])
                self.assertEqual(result.text, "streamed response")
                self.assertEqual(result.stop_reason, "stop")
            self.assertTrue(provider.closed)

    def test_leaving_sync_stream_early_cancels_the_run(self) -> None:
        provider = BlockingStreamingProvider()
        with patch("sec.apis.client.create_provider", return_value=provider):
            with SecClient(
                "assistant",
                "Follow the user.",
                provider="openai",
                model="test-model",
                api_key="test-key",
                session_store_backend="in_memory",
                env_file=None,
            ) as client:
                with client.stream("Hello") as stream:
                    first_text = next(
                        text_delta
                        for event in stream
                        if (text_delta := _event_text_delta(event)) is not None
                    )
                    self.assertEqual(first_text, "partial")

            self.assertTrue(provider.cancelled.is_set())
            self.assertTrue(provider.closed)


if __name__ == "__main__":
    unittest.main()
