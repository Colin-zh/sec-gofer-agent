"""验证工作上下文估算、压缩计划、摘要持久化及运行时接线。"""

from __future__ import annotations

import unittest
from collections.abc import Sequence

from sec._types.content import TextContent
from sec._types.events import CompactionEndEvent, CompactionStartEvent
from sec._types.messages import (
    AssistantMessage,
    Message,
    Usage,
    UsageCost,
    UserMessage,
)
from sec import ContextSettings, SessionStore
from sec.agent import Agent, Session
from sec._runtime.harness import Harness
from sec.config import Config, ConfigError, ProviderSettings
from sec.exceptions import ContextBuildError
from sec.memory import ContextBuilder
from sec.tools.tool import ToolDefinition


def _user_message(text: str, **metadata: object) -> UserMessage:
    return UserMessage([TextContent(text)], 1.0, metadata)


class LengthTokenEstimator:
    """以文本字符数作为测试替身的确定性估算器。"""

    def estimate_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> int:
        return sum(len(message.text) for message in messages) + sum(
            len(tool.name) for tool in tools
        )


class ScriptedProvider:
    """记录模型请求并按顺序返回预设答复。"""

    def __init__(self, *responses: AssistantMessage) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[Message, ...]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AssistantMessage:
        self.requests.append(tuple(messages))
        if not self.responses:
            raise AssertionError("provider has no scripted response left")
        return self.responses.pop(0)


def assistant_text(text: str) -> AssistantMessage:
    return _assistant_message([TextContent(text)])


def _assistant_message(
    content: list[TextContent],
    **metadata: object,
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
        metadata={"usage_reported": False, **metadata},
    )


class ContextBuilderTests(unittest.TestCase):
    """覆盖输入估算校验与压缩后上下文的边界。"""

    def test_estimator_must_return_a_non_negative_integer(self) -> None:
        class InvalidEstimator:
            def estimate_tokens(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> int:
                return True

        builder = ContextBuilder(
            InvalidEstimator(),  # type: ignore[arg-type]
            context_window_tokens=100,
            max_output_tokens=10,
            compression_threshold=0.8,
        )
        with self.assertRaisesRegex(ContextBuildError, "non-negative integer"):
            builder.prepare(
                system_prompt="instructions",
                session_messages=[],
                tools=(),
            )

    def test_compaction_keeps_recent_full_turns_and_persists_coverage(self) -> None:
        builder = ContextBuilder(
            LengthTokenEstimator(),
            context_window_tokens=1000,
            max_output_tokens=100,
            compression_threshold=0.5,
        )
        history = [
            _user_message("old request " + "a" * 240, turn_id="old"),
            _assistant_message(
                [TextContent(text="old response " + "b" * 220)],
                turn_id="old",
            ),
            _user_message("recent request " + "c" * 40, turn_id="recent"),
            _assistant_message(
                [TextContent(text="recent response " + "d" * 20)],
                turn_id="recent",
            ),
            _user_message("current request", turn_id="current"),
        ]

        plan = builder.plan_compaction(
            system_prompt="system",
            session_messages=history,
            tools=(),
        )

        self.assertEqual(plan.covers_through, 2)
        self.assertEqual(plan.retained_turns, 2)
        summary = ContextBuilder.create_summary_message(
            "Important facts from the old turn.",
            covers_through=plan.covers_through,
        )
        prepared = builder.prepare(
            system_prompt="system",
            session_messages=[*history, summary],
            tools=(),
        )
        self.assertEqual(prepared.summary_text, "Important facts from the old turn.")
        self.assertEqual(prepared.summary_covers_through, 2)
        self.assertNotIn(history[0], prepared.messages)
        self.assertIn(history[2], prepared.messages)
        self.assertIn(history[4], prepared.messages)
        self.assertEqual(summary.metadata["covers_through"], 2)

    def test_harness_requires_context_window_and_estimator_together(self) -> None:
        provider = ScriptedProvider()
        with self.assertRaisesRegex(ConfigError, "both context_window_tokens"):
            Harness(
                provider,
                context_settings=ContextSettings(context_window_tokens=1000),
            )
        with self.assertRaisesRegex(ConfigError, "both context_window_tokens"):
            Harness(provider, token_estimator=LengthTokenEstimator())

    def test_context_settings_load_from_environment(self) -> None:
        config = Config.from_env(
            None,
            environ={
                "OPENAI_MODEL": "test-model",
                "OPENAI_API_KEY": "test-secret",
                "SEC_CONTEXT_WINDOW_TOKENS": "8192",
                "SEC_CONTEXT_COMPRESSION_THRESHOLD": "0.65",
            },
        )

        self.assertEqual(config.context.context_window_tokens, 8192)
        self.assertEqual(config.context.compression_threshold, 0.65)

        with self.assertRaisesRegex(ConfigError, "SEC_CONTEXT_WINDOW_TOKENS"):
            Config.from_env(
                None,
                environ={
                    "OPENAI_MODEL": "test-model",
                    "OPENAI_API_KEY": "test-secret",
                    "SEC_CONTEXT_WINDOW_TOKENS": "0",
                },
            )

    def test_factory_passes_context_configuration_to_harness(self) -> None:
        from sec.apis.client import create_harness

        config = Config(
            provider=ProviderSettings(
                name="openai",
                model="test-model",
                api_key="test-secret",
                max_output_tokens=128,
            ),
            context=ContextSettings(
                context_window_tokens=2048,
                compression_threshold=0.7,
            ),
        )
        harness = create_harness(
            config,
            token_estimator=LengthTokenEstimator(),
        )

        self.assertIsNotNone(harness._context_builder)
        assert harness._context_builder is not None
        self.assertEqual(harness._context_builder.context_window_tokens, 2048)
        self.assertEqual(harness._context_builder.max_output_tokens, 128)


class ContextCompressionLoopTests(unittest.IsolatedAsyncioTestCase):
    """验证摘要调用预算、摘要持久化与最终答复提示。"""

    async def test_compaction_counts_model_call_and_prompts_final_response(self) -> None:
        provider = ScriptedProvider(
            assistant_text("Summary of earlier facts."),
            assistant_text("Final response."),
        )
        harness = Harness(
            provider,
            context_settings=ContextSettings(
                context_window_tokens=1000,
                compression_threshold=0.5,
            ),
            token_estimator=LengthTokenEstimator(),
            max_output_tokens=100,
        )
        store = SessionStore(backend="in_memory")
        session = Session(store=store)
        await session._append_message(
            _user_message("old request " + "a" * 240, turn_id="old")
        )
        await session._append_message(
            _assistant_message(
                [TextContent(text="old response " + "b" * 220)],
                turn_id="old",
            )
        )
        await session._append_message(
            _user_message("recent request " + "c" * 40, turn_id="recent")
        )
        await session._append_message(
            _assistant_message(
                [TextContent(text="recent response " + "d" * 20)],
                turn_id="recent",
            )
        )

        run = harness.run(
            Agent(name="assistant", instructions="system", max_model_calls=2),
            "current request",
            session=session,
        )
        result = await run.result()
        events = [event async for event in run.events()]

        self.assertEqual(result.model_calls, 2)
        self.assertTrue(
            any(
                event.type == "message_end"
                and event.message.metadata.get("_sec_context_summary") is True
                for event in events
            )
        )
        compaction_start = next(
            event for event in events if isinstance(event, CompactionStartEvent)
        )
        compaction_end = next(
            event for event in events if isinstance(event, CompactionEndEvent)
        )
        self.assertEqual(compaction_start.reason, "threshold")
        self.assertEqual(compaction_end.reason, "threshold")
        self.assertFalse(compaction_end.aborted)
        self.assertEqual(result.final_message.text, "Final response.")
        self.assertEqual(len(provider.requests), 2)
        self.assertTrue(
            any(
                "Persistent conversation memory" in message.text
                for message in provider.requests[1]
            )
        )
        self.assertTrue(
            any(
                "final answer now" in message.text
                for message in provider.requests[1]
            )
        )
        summary_message = next(
            message
            for message in session.messages
            if message.metadata.get("_sec_context_summary") is True
        )
        self.assertEqual(summary_message.text, "Summary of earlier facts.")
        restored = await Session.restore(store, session.id)
        self.assertEqual(
            next(
                message
                for message in restored.messages
                if message.metadata.get("_sec_context_summary") is True
            ).metadata["covers_through"],
            summary_message.metadata["covers_through"],
        )

    async def test_insufficient_call_budget_skips_summary_and_requests_final(self) -> None:
        provider = ScriptedProvider(assistant_text("Final response."))
        harness = Harness(
            provider,
            context_settings=ContextSettings(
                context_window_tokens=1000,
                compression_threshold=0.5,
            ),
            token_estimator=LengthTokenEstimator(),
            max_output_tokens=100,
        )
        session = Session()
        await session._append_message(
            _user_message("old request " + "a" * 240, turn_id="old")
        )
        await session._append_message(
            _assistant_message(
                [TextContent(text="old response " + "b" * 220)],
                turn_id="old",
            )
        )
        await session._append_message(
            _user_message("recent request " + "c" * 40, turn_id="recent")
        )
        await session._append_message(
            _assistant_message(
                [TextContent(text="recent response " + "d" * 20)],
                turn_id="recent",
            )
        )

        result = await harness.run(
            Agent(name="assistant", instructions="system", max_model_calls=1),
            "current request",
            session=session,
        ).result()

        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.final_message.text, "Final response.")
        self.assertEqual(len(provider.requests), 1)
        self.assertTrue(
            any(
                "final answer now" in message.text
                for message in provider.requests[0]
            )
        )
        self.assertFalse(
            any(
                message.metadata.get("_sec_context_summary") is True
                for message in session.messages
            )
        )


if __name__ == "__main__":
    unittest.main()
