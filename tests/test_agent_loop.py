"""验证 sec Agent 循环、工具调用、运行控制及事件流行为。

测试使用 ScriptedProvider 返回预先安排的响应，确保模型—工具—模型流程及
错误、预算、取消和事件交付语义可离线、确定性地验证。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch
from collections.abc import AsyncIterator, Sequence
from typing import Any

from sec._types.content import TextContent, ToolUseContent
from sec._types.events import (
    AgentSettledEvent,
    AssistantDoneEvent,
    AutoRetryEndEvent,
    ContentDeltaEvent,
    EmptySessionEvent,
    MessageUpdateEvent,
    ModelRequestEndEvent,
    ModelRequestStartEvent,
    RetryStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from sec._types.messages import (
    AssistantMessage,
    Message,
    ToolResultMessage,
    Usage,
    UsageCost,
    message_to_dict,
)
from sec import (
    AllowListPolicy,
    RetrySettings,
    tool,
)
from sec.agent import Agent, Session
from sec._runtime.harness import Harness
from sec.exceptions import (
    ProviderProtocolError,
    RetryableProviderError,
    RunLimitExceeded,
    SecSubscriptionOverflow,
    SessionBusyError,
)
from sec._runtime.event_stream import RunEventStream
from sec._types.providers import StreamingModelProvider
from sec._types.streaming import (
    ModelResponseCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)
from sec.tools.tool import ToolDefinition
from sec.tools import ToolRegistry


class ScriptedProvider:
    """按测试给定顺序返回消息并记录每次模型请求。"""

    def __init__(self, *responses: AssistantMessage) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[tuple[Message, ...], tuple[ToolDefinition, ...]]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AssistantMessage:
        self.requests.append((tuple(messages), tuple(tools)))
        if not self._responses:
            raise AssertionError("provider has no scripted response left")
        return self._responses.pop(0)


def _assistant_message(
    content: list[TextContent | ToolUseContent],
    *,
    usage: dict[str, int] | None = None,
) -> AssistantMessage:
    raw_usage = usage or {}
    input_tokens = raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0))
    output_tokens = raw_usage.get(
        "output_tokens",
        raw_usage.get("completion_tokens", 0),
    )
    normalized_usage = Usage(
        input_tokens,
        output_tokens,
        0,
        0,
        input_tokens + output_tokens,
        UsageCost(0, 0, 0, 0, 0),
    )
    return AssistantMessage(
        content,
        "test-api",
        "test-provider",
        "test-model",
        normalized_usage,
        "stop",
        1.0,
        metadata={"usage_reported": usage is not None},
    )


def assistant_text(text: str) -> AssistantMessage:
    return _assistant_message([TextContent(text=text)])


def tool_request(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "call-1",
) -> AssistantMessage:
    return _assistant_message(
        [ToolUseContent(id=call_id, name=tool_name, arguments=arguments)]
    )


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    """覆盖 Agent 执行循环、策略、预算和控制行为。"""

    async def test_runs_model_tool_model_and_returns_final_message(self) -> None:
        executed: list[int] = []

        @tool
        def add(value: int) -> int:
            executed.append(value)
            return value + 1

        provider = ScriptedProvider(
            tool_request("add", {"value": 4}),
            assistant_text("5"),
        )
        harness = Harness(provider, policy=AllowListPolicy({"add"}))
        agent = Agent(name="calculator", instructions="Use tools.", tools=(add,))

        run = harness.run(agent, "Add one to four")
        result = await run.result()
        events = [event async for event in run.events()]

        self.assertEqual(result.final_message.text, "5")
        self.assertEqual((result.model_calls, result.tool_calls), (2, 1))
        self.assertEqual(executed, [4])
        self.assertEqual(provider.requests[0][1][0].name, "add")
        self.assertEqual(
            provider.requests[0][1][0].input_schema["properties"]["value"],
            {"type": "integer"},
        )
        self.assertEqual(
            provider.requests[0][1][0].output_schema,
            {"type": "integer"},
        )
        tool_results = provider.requests[1][0][-1]
        self.assertEqual(tool_results.tool_call_id, "call-1")
        self.assertEqual(tool_results.text, "5")
        starts = [event for event in events if isinstance(event, ModelRequestStartEvent)]
        ends = [event for event in events if isinstance(event, ModelRequestEndEvent)]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(ends), 2)
        for start, request in zip(starts, provider.requests):
            self.assertEqual(start.input["messages"], [
                message_to_dict(message) for message in request[0]
            ])
            self.assertEqual(start.input["tools"][0]["name"], "add")
        self.assertEqual(ends[0].response.tool_uses[0].name, "add")
        self.assertEqual(ends[1].response.text, "5")
        self.assertNotEqual(starts[0].request_id, starts[1].request_id)
        self.assertEqual(starts[0].turn_id, starts[1].turn_id)

    async def test_model_attempt_duration_and_snapshots_are_independent_of_mutation(self) -> None:
        response = assistant_text("original")
        provider = ScriptedProvider(response)
        with patch(
            "sec._runtime.model_generation.monotonic_ns",
            side_effect=[1_000_000, 13_500_000],
        ):
            run = Harness(provider).run(
                Agent(name="snapshot", instructions="system instructions"), "question",
            )
            await run.result()
        events = [event async for event in run.events()]
        start = next(event for event in events if isinstance(event, ModelRequestStartEvent))
        end = next(event for event in events if isinstance(event, ModelRequestEndEvent))
        self.assertEqual(end.elapsed_ms, 12.5)
        response.content.clear()
        provider.requests[0][0][0].content = "mutated system"
        self.assertEqual(start.input["messages"][0]["content"][0]["text"], "system instructions")
        self.assertEqual(end.response.text, "original")
        self.assertEqual(start.request_id, end.request_id)
        self.assertLess(events.index(start), events.index(end))

    async def test_policy_denial_is_returned_without_executing_tool(self) -> None:
        executed = False

        @tool
        def delete_record(record_id: str) -> str:
            nonlocal executed
            executed = True
            return record_id

        provider = ScriptedProvider(
            tool_request("delete_record", {"record_id": "r-1"}),
            assistant_text("I cannot do that."),
        )
        harness = Harness(provider)
        agent = Agent(
            name="safe-agent",
            instructions="Follow policy.",
            tools=(delete_record,),
        )

        result = await harness.run(agent, "Delete r-1").result()

        denied_result = provider.requests[1][0][-1]
        self.assertFalse(executed)
        self.assertTrue(denied_result.is_error)
        self.assertEqual(denied_result.text, "Tool call denied by policy.")
        self.assertEqual(result.final_message.text, "I cannot do that.")

    async def test_provider_can_return_final_answer_without_tools(self) -> None:
        provider = ScriptedProvider(assistant_text("Hello"))
        harness = Harness(provider)
        agent = Agent(name="greeter", instructions="Greet briefly.")

        result = await harness.run(agent, "Hi").result()

        self.assertEqual(result.final_message.text, "Hello")
        self.assertEqual((result.model_calls, result.tool_calls), (1, 0))

    async def test_provider_retry_attempts_do_not_spend_model_call_budget(self) -> None:
        class FlakyProvider:
            """先返回暂时性错误、随后成功的模型替身。"""

            def __init__(self) -> None:
                self.attempts = 0

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.attempts += 1
                if self.attempts < 3:
                    raise RetryableProviderError("temporary failure")
                return _assistant_message(
                    [TextContent(text="recovered")],
                    usage={"prompt_tokens": 6, "completion_tokens": 2},
                )

        provider = FlakyProvider()
        harness = Harness(
            provider,
            retry_settings=RetrySettings(
                max_attempts=3,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        )
        run = harness.run(
            Agent(
                name="retry",
                instructions="",
                max_model_calls=1,
            ),
            "Answer",
        )

        result = await run.result()
        events = [event async for event in run.events()]

        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.model_attempts, 3)
        self.assertEqual(result.retries, 2)
        self.assertEqual(provider.attempts, 3)
        self.assertEqual(result.usage.input_tokens, 6)
        self.assertEqual(result.usage.output_tokens, 2)
        self.assertFalse(result.usage.is_complete)
        self.assertEqual(result.usage.unreported_attempts, 2)
        self.assertEqual(
            [event.type for event in events].count("auto_retry_start"),
            2,
        )
        self.assertEqual(events[-1].type, "agent_settled")
        starts = [event for event in events if isinstance(event, ModelRequestStartEvent)]
        ends = [event for event in events if isinstance(event, ModelRequestEndEvent)]
        self.assertEqual([event.attempt for event in starts], [1, 2, 3])
        self.assertEqual([event.status for event in ends], ["failed", "failed", "completed"])
        self.assertEqual(len({event.request_id for event in starts}), 1)
        self.assertEqual(
            [(event.request_id, event.attempt) for event in starts],
            [(event.request_id, event.attempt) for event in ends],
        )
        self.assertEqual(ends[0].error, "temporary failure")
        self.assertEqual(ends[-1].response.text, "recovered")
        self.assertTrue(all(event.elapsed_ms >= 0 for event in ends))
        retry_index = next(i for i, event in enumerate(events) if event.type == "auto_retry_start")
        self.assertLess(events.index(ends[0]), retry_index)

    async def test_model_retry_does_not_replay_a_completed_tool_call(self) -> None:
        executed: list[int] = []
        requests: list[tuple[Message, ...]] = []

        @tool
        def record(value: int) -> int:
            executed.append(value)
            return value

        class Provider:
            """记录请求次数并返回固定消息的模型替身。"""

            def __init__(self) -> None:
                self.calls = 0

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.calls += 1
                requests.append(tuple(messages))
                if self.calls == 1:
                    return tool_request("record", {"value": 7})
                if self.calls == 2:
                    raise RetryableProviderError("temporary failure")
                return assistant_text("Recorded")

        provider = Provider()
        run = Harness(
            provider,
            policy=AllowListPolicy({"record"}),
            retry_settings=RetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        ).run(
            Agent(
                name="no-tool-replay",
                instructions="",
                tools=(record,),
                max_model_calls=2,
            ),
            "Record seven",
        )

        result = await run.result()

        self.assertEqual(result.final_message.text, "Recorded")
        self.assertEqual((result.model_calls, result.model_attempts, result.retries), (2, 3, 1))
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(executed, [7])
        self.assertEqual(
            requests[2][-1].tool_call_id,
            requests[1][-1].tool_call_id,
        )

    async def test_cancel_during_retry_backoff_prevents_next_attempt(self) -> None:
        class TemporarilyUnavailableProvider:
            """持续报告暂时不可用的模型替身。"""

            def __init__(self) -> None:
                self.calls = 0

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.calls += 1
                raise RetryableProviderError("temporarily unavailable")

        provider = TemporarilyUnavailableProvider()
        run = Harness(
            provider,
            retry_settings=RetrySettings(
                max_attempts=3,
                base_delay_seconds=1,
                max_delay_seconds=1,
            ),
        ).run(Agent(name="cancel-retry", instructions=""), "Answer")
        event_iterator = run.events()
        while True:
            event = await asyncio.wait_for(anext(event_iterator), timeout=1)
            if event.type == "auto_retry_start":
                break

        self.assertTrue(run.cancel())
        with self.assertRaises(asyncio.CancelledError):
            await run.result()
        remaining_events = [event async for event in event_iterator]

        self.assertEqual(provider.calls, 1)
        terminal = remaining_events[-1]
        self.assertIsInstance(terminal, AgentSettledEvent)
        self.assertEqual(terminal.status, "cancelled")
        replay = [event async for event in run.events()]
        starts = [event for event in replay if isinstance(event, ModelRequestStartEvent)]
        ends = [event for event in replay if isinstance(event, ModelRequestEndEvent)]
        self.assertEqual(len(starts), 1)
        self.assertEqual([event.status for event in ends], ["failed"])

    async def test_non_retryable_provider_error_is_not_retried(self) -> None:
        class InvalidProvider:
            """返回不符合 Provider 协议消息的模型替身。"""

            def __init__(self) -> None:
                self.calls = 0

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.calls += 1
                raise ValueError("invalid request")

        provider = InvalidProvider()
        run = Harness(
            provider,
            retry_settings=RetrySettings(
                max_attempts=4,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        ).run(Agent(name="no-retry", instructions=""), "Answer")

        with self.assertRaisesRegex(ValueError, "invalid request"):
            await run.result()
        events = [event async for event in run.events()]
        self.assertEqual(provider.calls, 1)
        self.assertIsInstance(events[-1], AgentSettledEvent)
        self.assertEqual(events[-1].status, "failed")
        ends = [event for event in events if isinstance(event, ModelRequestEndEvent)]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].status, "failed")
        self.assertEqual(ends[0].error, "invalid request")
        self.assertIsNone(ends[0].response)

    async def test_retry_exhaustion_reports_attempt_counts(self) -> None:
        class UnavailableProvider:
            """记录调用后持续返回不可用错误的模型替身。"""

            def __init__(self) -> None:
                self.calls = 0

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.calls += 1
                raise RetryableProviderError("service unavailable", status_code=503)

        provider = UnavailableProvider()
        run = Harness(
            provider,
            retry_settings=RetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        ).run(Agent(name="exhausted", instructions=""), "Answer")

        with self.assertRaises(RetryableProviderError):
            await run.result()
        events = [event async for event in run.events()]
        self.assertEqual(provider.calls, 2)
        self.assertEqual(
            [event.type for event in events].count("auto_retry_start"),
            1,
        )
        self.assertEqual(events[-1].type, "agent_settled")

    async def test_tool_exception_propagates_to_caller(self) -> None:
        @tool
        def fail() -> str:
            raise ValueError("tool failed")

        provider = ScriptedProvider(tool_request("fail", {}))
        harness = Harness(provider, policy=AllowListPolicy({"fail"}))
        agent = Agent(name="failure-test", instructions="", tools=(fail,))
        session = Session()
        run = harness.run(agent, "Fail", session=session)

        with self.assertRaisesRegex(ValueError, "tool failed"):
            await run.result()
        results = session.messages[-1]
        self.assertEqual(results.tool_call_id, "call-1")
        self.assertTrue(results.is_error)

    async def test_invalid_tool_argument_is_rejected_before_execution(self) -> None:
        executed = False

        @tool
        def add(value: int) -> int:
            nonlocal executed
            executed = True
            return value + 1

        provider = ScriptedProvider(
            tool_request("add", {"value": "4"}),
            assistant_text("The argument must be an integer."),
        )
        harness = Harness(provider, policy=AllowListPolicy({"add"}))
        agent = Agent(name="typed", instructions="", tools=(add,))

        run = harness.run(agent, "Add one")
        result = await run.result()
        feedback = provider.requests[1][0][-1]

        self.assertFalse(executed)
        self.assertEqual(result.final_message.text, "The argument must be an integer.")
        self.assertTrue(feedback.is_error)
        self.assertEqual(feedback.metadata["error_kind"], "invalid_arguments")
        self.assertIn("must have JSON type integer", feedback.text)

    async def test_run_can_cancel_pending_async_provider_call(self) -> None:
        started = asyncio.Event()

        class BlockingProvider:
            """阻塞模型响应以验证 Run 等待和超时行为。"""

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                started.set()
                await asyncio.Event().wait()
                return assistant_text("unreachable")

        harness = Harness(BlockingProvider())
        agent = Agent(name="cancellable", instructions="")
        run = harness.run(agent, "Wait")
        await started.wait()

        self.assertTrue(run.cancel())
        with self.assertRaises(asyncio.CancelledError):
            await run.result()
        self.assertTrue(run.done)
        events = [event async for event in run.events()]
        self.assertIsInstance(events[-1], AgentSettledEvent)
        self.assertEqual(events[-1].status, "cancelled")
        ends = [event for event in events if isinstance(event, ModelRequestEndEvent)]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].status, "cancelled")
        self.assertIsNone(ends[0].response)

    async def test_steering_interrupts_uncommitted_generation(self) -> None:
        class InterruptibleProvider:
            """允许测试在模型请求期间观察并中断运行的替身。"""

            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.requests: list[tuple[Message, ...]] = []

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.requests.append(tuple(messages))
                if len(self.requests) == 1:
                    self.started.set()
                    await asyncio.Event().wait()
                return assistant_text("Steered response")

        provider = InterruptibleProvider()
        session = Session()
        run = Harness(provider).run(
            Agent(name="steerable", instructions=""),
            "Start",
            session=session,
        )
        await provider.started.wait()

        self.assertTrue(run.steer("Change direction"))
        result = await run.result()

        self.assertEqual(result.final_message.text, "Steered response")
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(
            [message.text for message in session.messages if message.role == "user"],
            ["Start", "Change direction"],
        )
        self.assertEqual(
            [message.text for message in session.messages if message.role == "assistant"],
            ["Steered response"],
        )
        events = [event async for event in run.events()]
        ends = [event for event in events if isinstance(event, ModelRequestEndEvent)]
        self.assertEqual([event.status for event in ends], ["cancelled", "completed"])
        self.assertNotEqual(ends[0].request_id, ends[1].request_id)
        self.assertEqual(ends[0].turn_id, ends[1].turn_id)

    async def test_response_completed_during_steering_is_committed_first(self) -> None:
        class CancellationResistantProvider:
            """模拟取消请求后仍继续运行的 Provider。"""

            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.calls = 0

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        return assistant_text("Completed before steering commit")
                return assistant_text("Final response")

        provider = CancellationResistantProvider()
        session = Session()
        run = Harness(provider).run(
            Agent(name="race", instructions=""),
            "Start",
            session=session,
        )
        await provider.started.wait()
        self.assertTrue(run.steer("Continue with this direction"))

        result = await run.result()

        assistant_texts = [
            message.text
            for message in session.messages
            if message.role == "assistant"
        ]
        self.assertEqual(
            assistant_texts,
            ["Completed before steering commit", "Final response"],
        )
        self.assertEqual(result.model_calls, 2)

    async def test_follow_up_starts_new_turn_with_fresh_budget(self) -> None:
        class FollowUpProvider:
            """暂停后续响应以便测试 steering 消息的提交时序。"""

            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = 0

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    await self.release.wait()
                    return assistant_text("First turn")
                return assistant_text("Second turn")

        provider = FollowUpProvider()
        session = Session()
        run = Harness(provider).run(
            Agent(name="follow-up", instructions="", max_model_calls=1),
            "First prompt",
            session=session,
        )
        await provider.started.wait()
        self.assertTrue(run.follow_up("A separate follow-up"))
        provider.release.set()

        result = await run.result()

        self.assertEqual(result.final_message.text, "Second turn")
        self.assertEqual(result.model_calls, 2)
        user_messages = [
            message
            for message in session.messages
            if message.role == "user"
        ]
        self.assertEqual([message.text for message in user_messages], [
            "First prompt",
            "A separate follow-up",
        ])
        self.assertNotEqual(
            user_messages[0].metadata["turn_id"],
            user_messages[1].metadata["turn_id"],
        )

    async def test_stop_during_tool_waits_for_tool_result_commit(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        @tool
        async def slow() -> str:
            started.set()
            await release.wait()
            return "completed"

        provider = ScriptedProvider(tool_request("slow", {}))
        session = Session()
        run = Harness(
            provider,
            policy=AllowListPolicy({"slow"}),
        ).run(
            Agent(name="safe-stop", instructions="", tools=(slow,)),
            "Run slow tool",
            session=session,
        )
        await started.wait()

        self.assertTrue(run.cancel())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await run.result()

        tool_result_messages = [
            message
            for message in session.messages
            if isinstance(message, ToolResultMessage)
        ]
        self.assertEqual(len(tool_result_messages), 1)
        self.assertEqual(
            tool_result_messages[0].text,
            "completed",
        )
        self.assertEqual(len(provider.requests), 1)

    async def test_session_rejects_a_second_active_run(self) -> None:
        started = asyncio.Event()

        class BlockingProvider:
            """阻塞模型响应以验证取消时的事件和状态清理。"""

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                started.set()
                await asyncio.Event().wait()
                return assistant_text("unreachable")

        session = Session()
        harness = Harness(BlockingProvider())
        run = harness.run(Agent(name="one-at-a-time", instructions=""), "First", session=session)
        await started.wait()

        with self.assertRaises(SessionBusyError):
            harness.run(Agent(name="one-at-a-time", instructions=""), "Second", session=session)
        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run.result()

    async def test_run_limit_is_reported_as_an_exception(self) -> None:
        @tool
        def noop() -> str:
            return "done"

        provider = ScriptedProvider(
            tool_request("noop", {}, call_id="call-1"),
            tool_request("noop", {}, call_id="call-2"),
        )
        harness = Harness(provider, policy=AllowListPolicy({"noop"}))
        session = Session()
        agent = Agent(
            name="limited",
            instructions="",
            tools=(noop,),
            max_model_calls=2,
        )
        run = harness.run(agent, "Keep going", session=session)

        with self.assertRaises(RunLimitExceeded):
            await run.result()
        events = [event async for event in run.events()]
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            session.messages[-1].tool_call_id,
            "call-2",
        )
        self.assertIsInstance(events[-1], AgentSettledEvent)
        self.assertEqual(events[-1].status, "limit_reached")

    async def test_tool_call_limit_stops_additional_calls_in_same_response(self) -> None:
        executed: list[int] = []

        @tool
        def record(value: int) -> int:
            executed.append(value)
            return value

        provider = ScriptedProvider(
            _assistant_message(
                [
                    ToolUseContent(id="call-1", name="record", arguments={"value": 1}),
                    ToolUseContent(id="call-2", name="record", arguments={"value": 2}),
                ]
            )
        )
        harness = Harness(provider, policy=AllowListPolicy({"record"}))
        session = Session()
        agent = Agent(
            name="bounded",
            instructions="",
            tools=(record,),
            max_tool_calls=1,
        )

        with self.assertRaisesRegex(RunLimitExceeded, "tool calls"):
            await harness.run(agent, "Record", session=session).result()
        self.assertEqual(executed, [1])
        self.assertEqual(
            [message.tool_call_id for message in session.messages[-2:]],
            ["call-1", "call-2"],
        )


class ToolRegistryTests(unittest.TestCase):
    """覆盖工具注册、查找及重复名称保护。"""

    def test_registry_stores_and_resolves_authoritative_tool(self) -> None:
        @tool
        def greet(name: str) -> str:
            return f"Hello, {name}"

        registry = ToolRegistry()
        registry.register(greet)

        self.assertIs(registry.get("greet"), greet)
        self.assertEqual(registry.definitions(), (greet.definition,))

    def test_registry_rejects_duplicate_tool_names(self) -> None:
        @tool(name="same")
        def first() -> str:
            return "first"

        @tool(name="same")
        def second() -> str:
            return "second"

        registry = ToolRegistry()
        registry.register(first)

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(second)


class SessionEventTests(unittest.IsolatedAsyncioTestCase):
    """Verify Run emits the new SessionEvent protocol and preserves stream behavior."""

    async def test_tool_turn_emits_session_protocol_events(self) -> None:
        @tool
        def greet(name: str) -> str:
            return f"Hello, {name}"

        provider = ScriptedProvider(
            tool_request("greet", {"name": "Ada"}, call_id="call-greet"),
            assistant_text("Done"),
        )
        run = Harness(
            provider,
            policy=AllowListPolicy({"greet"}),
        ).run(
            Agent(name="event-test", instructions="", tools=(greet,)),
            "Greet Ada",
        )

        result = await run.result()
        events = [event async for event in run.events()]
        event_types = [event.type for event in events]

        self.assertEqual(result.final_message.text, "Done")
        self.assertEqual(event_types[0], "agent_start")
        self.assertIn("turn_start", event_types)
        self.assertIn("message_start", event_types)
        self.assertIn("message_end", event_types)
        self.assertIn("turn_end", event_types)
        self.assertIn("agent_end", event_types)
        self.assertEqual(event_types[-1], "agent_settled")

        tool_start = next(
            event for event in events if isinstance(event, ToolExecutionStartEvent)
        )
        tool_end = next(
            event for event in events if isinstance(event, ToolExecutionEndEvent)
        )
        self.assertEqual(tool_start.tool_call_id, "call-greet")
        self.assertEqual(tool_start.args, {"name": "Ada"})
        self.assertEqual(tool_end.tool_call_id, "call-greet")
        self.assertEqual(tool_end.result, "Hello, Ada")
        self.assertFalse(tool_end.is_error)

    async def test_streaming_provider_publishes_protocol_deltas_live(self) -> None:
        class PausingStreamingProvider:
            def __init__(self) -> None:
                self.release = asyncio.Event()

            async def stream(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> AsyncIterator[ModelStreamEvent]:
                yield ModelTextDelta("Hello")
                await self.release.wait()
                yield ModelTextDelta(" world")
                yield ModelResponseCompleted(assistant_text("Hello world"))

        raw_provider = PausingStreamingProvider()
        provider: StreamingModelProvider = raw_provider
        run = Harness(provider).run(
            Agent(name="streaming", instructions=""),
            "Say hello",
        )
        event_iterator = run.events()
        events = []
        while True:
            event = await asyncio.wait_for(anext(event_iterator), timeout=1)
            events.append(event)
            if (
                isinstance(event, MessageUpdateEvent)
                and isinstance(event.assistant_message_event, ContentDeltaEvent)
                and event.assistant_message_event.type == "text_delta"
            ):
                break

        self.assertIsInstance(events[-1], MessageUpdateEvent)
        self.assertIsInstance(events[-1].assistant_message_event, ContentDeltaEvent)
        self.assertEqual(events[-1].assistant_message_event.delta, "Hello")
        self.assertFalse(run.done)
        self.assertTrue(any(isinstance(event, ModelRequestStartEvent) for event in events))
        self.assertFalse(any(isinstance(event, ModelRequestEndEvent) for event in events))
        raw_provider.release.set()

        result = await run.result()
        events.extend([event async for event in event_iterator])
        request_end = next(
            event for event in events if isinstance(event, ModelRequestEndEvent)
        )
        self.assertEqual(request_end.status, "completed")
        self.assertEqual(request_end.response.text, "Hello world")
        deltas = [
            event.assistant_message_event.delta
            for event in events
            if isinstance(event, MessageUpdateEvent)
            and isinstance(event.assistant_message_event, ContentDeltaEvent)
            and event.assistant_message_event.type == "text_delta"
        ]
        done_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, MessageUpdateEvent)
            and isinstance(event.assistant_message_event, AssistantDoneEvent)
        )
        self.assertEqual(result.final_message.text, "Hello world")
        self.assertEqual(deltas, ["Hello", " world"])
        self.assertLess(
            next(
                i
                for i, event in enumerate(events)
                if isinstance(event, MessageUpdateEvent)
                and isinstance(event.assistant_message_event, ContentDeltaEvent)
                and event.assistant_message_event.type == "text_delta"
            ),
            done_index,
        )
        self.assertEqual(events[-1].type, "agent_settled")

    async def test_retry_uses_new_protocol_retry_events(self) -> None:
        class FlakyProvider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                self.calls += 1
                if self.calls == 1:
                    raise RetryableProviderError("temporary failure")
                return assistant_text("recovered")

        provider = FlakyProvider()
        run = Harness(
            provider,
            retry_settings=RetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        ).run(Agent(name="retry-events", instructions=""), "Answer")

        result = await run.result()
        events = [event async for event in run.events()]
        retries = [
            event
            for event in events
            if isinstance(event, RetryStartEvent)
            and event.type == "auto_retry_start"
        ]
        retry_end = next(
            event for event in events if isinstance(event, AutoRetryEndEvent)
        )

        self.assertEqual(result.final_message.text, "recovered")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].error_message, "temporary failure")
        self.assertTrue(retry_end.success)
        self.assertEqual(events[-1].type, "agent_settled")

    async def test_late_subscriber_gets_overflow_after_history_is_truncated(self) -> None:
        run = Harness(
            ScriptedProvider(assistant_text("Done")),
            event_buffer_size=3,
        ).run(Agent(name="bounded-events", instructions=""), "Finish")

        result = await run.result()

        self.assertEqual(result.final_message.text, "Done")
        with self.assertRaises(SecSubscriptionOverflow):
            await anext(run.events())

    async def test_run_event_subscribers_have_independent_replay(self) -> None:
        run = Harness(ScriptedProvider(assistant_text("Done"))).run(
            Agent(name="multi-subscriber", instructions=""),
            "Finish",
        )
        await run.result()
        first = [event async for event in run.events()]
        second = [event async for event in run.events()]
        self.assertEqual(first, second)
        self.assertIsInstance(first[-1], AgentSettledEvent)
        self.assertEqual(first[-1].status, "completed")

    async def test_slow_run_event_subscriber_overflows_without_affecting_others(self) -> None:
        stream = RunEventStream(run_id="independent-subscribers", capacity=1)
        slow = stream.subscribe()
        active = stream.subscribe()
        started = EmptySessionEvent("agent_start")
        settled = AgentSettledEvent(status="completed")

        stream.publish(started)
        self.assertIs(await anext(active), started)
        stream.publish(settled)

        with self.assertRaises(SecSubscriptionOverflow):
            await anext(slow)
        self.assertIs(await anext(active), settled)
        with self.assertRaises(StopAsyncIteration):
            await anext(active)
        stream.close()

    async def test_extension_hooks_run_around_native_run(self) -> None:
        received = []

        async def before_run(context) -> None:
            received.append(("before", context.run_id, context.prompt))

        def on_event(event) -> None:
            received.append(("event", event.type))

        async def after_run(outcome) -> None:
            received.append(("after", outcome.status, outcome.result is not None))

        run = Harness(
            ScriptedProvider(assistant_text("Hooked")),
            before_run_hooks=(before_run,),
            after_run_hooks=(after_run,),
            run_event_hooks=(on_event,),
        ).run(Agent(name="hooked", instructions=""), "Run")
        result = await run.result()

        self.assertEqual(result.final_message.text, "Hooked")
        self.assertEqual(received[0][0], "before")
        self.assertEqual(received[0][2], "Run")
        self.assertEqual(received[-1], ("after", "completed", True))
        self.assertIn(("event", "agent_start"), received)

    async def test_extension_event_hook_failure_fails_run_explicitly(self) -> None:
        def broken_hook(_event) -> None:
            raise ValueError("extension hook failed")

        run = Harness(
            ScriptedProvider(assistant_text("Never returned")),
            run_event_hooks=(broken_hook,),
        ).run(Agent(name="broken-hook", instructions=""), "Run")

        with self.assertRaisesRegex(ExceptionGroup, "Run event hooks failed"):
            await run.result()
        events = [event async for event in run.events()]
        self.assertEqual(events[-1].type, "agent_settled")
        self.assertEqual(events[-1].status, "failed")

    async def test_failed_run_emits_settled_event_and_still_raises(self) -> None:
        class FailingProvider:
            async def complete(
                self,
                messages: Sequence[Message],
                tools: Sequence[ToolDefinition],
            ) -> Message:
                raise ValueError("provider unavailable")

        run = Harness(FailingProvider()).run(
            Agent(name="failed-event", instructions=""),
            "Fail",
        )
        with self.assertRaisesRegex(ValueError, "provider unavailable"):
            await run.result()

        events = [event async for event in run.events()]
        self.assertIsInstance(events[-1], AgentSettledEvent)
        self.assertEqual(events[-1].status, "failed")


if __name__ == "__main__":
    unittest.main()
