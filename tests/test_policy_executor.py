"""验证 sec Policy 决策、工具执行后端及 Run 审批控制契约。

测试通过确定性 Provider 和记录型 Executor 检查授权边界、审批暂停、
策略协议、工具结果关联及取消行为，不访问外部服务。
"""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence
from typing import Any

from sec._types.content import TextContent, ToolUseContent
from sec._types.events import (
    AssistantDoneEvent,
    MessageUpdateEvent,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolRetryEndEvent,
    ToolRetryStartEvent,
)
from sec._types.messages import (
    AssistantMessage,
    Message,
    ToolResultMessage,
    Usage,
    UsageCost,
)
from sec import (
    AllowListPolicy,
    DenyAllPolicy,
    LocalToolExecutor,
    PolicyContext,
    PolicyDecision,
    RecoverableToolError,
    RetryableToolError,
    ToolRetrySettings,
    ToolExecutorProtocolError,
    tool,
)
from sec.agent import Agent, Session
from sec._runtime.harness import Harness
from sec._types.policy import PolicyEngine
from sec.tools.tool import Tool, ToolDefinition


def _assistant_message(content: list[TextContent | ToolUseContent]) -> AssistantMessage:
    usage = Usage(0, 0, 0, 0, 0, UsageCost(0, 0, 0, 0, 0))
    return AssistantMessage(
        content,
        "test-api",
        "test-provider",
        "test-model",
        usage,
        "stop",
        1.0,
    )


def _tool_result(
    tool_call_id: str,
    tool_name: str,
    content: str,
    *,
    is_error: bool = False,
) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id,
        tool_name,
        [TextContent(content)],
        is_error,
        1.0,
    )


def _is_tool_call_completion(event: object) -> bool:
    return (
        isinstance(event, MessageUpdateEvent)
        and isinstance(event.assistant_message_event, AssistantDoneEvent)
        and bool(event.assistant_message_event.message.tool_uses)
    )


class SequenceProvider:
    """逐条返回脚本化模型响应并保留提交给模型的历史。"""

    def __init__(self, *responses: AssistantMessage) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[tuple[Message, ...], tuple[ToolDefinition, ...]]] = []

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AssistantMessage:
        self.requests.append((tuple(messages), tuple(tools)))
        if not self.responses:
            raise AssertionError("no scripted response remains")
        return self.responses.pop(0)


class RecordingExecutor:
    """记录已获准的工具执行并返回配对结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Tool, ToolUseContent]] = []

    async def execute(
        self,
        registered: Tool,
        tool_use: ToolUseContent,
    ) -> ToolResultMessage:
        self.calls.append((registered, tool_use))
        return _tool_result(tool_use.id, tool_use.name, "executor-result")


def policy_context(
    capability_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    agent_name: str = "test-agent",
    mode_name: str | None = None,
    source: str | None = "local",
) -> PolicyContext:
    """创建策略测试使用的能力上下文。"""
    return PolicyContext(
        agent_name=agent_name,
        mode_name=mode_name,
        capability_type="tool",
        capability_name=capability_name,
        source=source,
        arguments={} if arguments is None else arguments,
    )


class PolicyDecisionTests(unittest.TestCase):
    """覆盖三态决策、不变量及内置策略行为。"""

    def test_decisions_support_allow_ask_and_deny(self) -> None:
        self.assertEqual(PolicyDecision.allow().outcome, "allow")
        self.assertEqual(PolicyDecision.ask("Confirm").outcome, "ask")
        self.assertEqual(PolicyDecision.deny("Blocked").outcome, "deny")

        with self.assertRaisesRegex(ValueError, "require a reason"):
            PolicyDecision("ask")
        with self.assertRaisesRegex(ValueError, "require a reason"):
            PolicyDecision("deny")
        with self.assertRaisesRegex(ValueError, "must be"):
            PolicyDecision("maybe")  # type: ignore[arg-type]

    def test_builtin_policies_return_explicit_decisions(self) -> None:
        allow = AllowListPolicy({"read"})
        self.assertEqual(
            allow.decide(policy_context("read")).outcome,
            "allow",
        )
        denied = allow.decide(policy_context("write"))
        self.assertEqual(denied.outcome, "deny")
        self.assertTrue(denied.reason)
        self.assertEqual(
            DenyAllPolicy().decide(policy_context("read")).outcome,
            "deny",
        )

    def test_context_preserves_agent_mode_and_capability_scope(self) -> None:
        context = policy_context(
            "read",
            {"path": "README.md"},
            agent_name="reviewer",
            mode_name="readonly",
            source="mcp:docs",
        )

        self.assertEqual(context.agent_name, "reviewer")
        self.assertEqual(context.mode_name, "readonly")
        self.assertEqual(context.capability_name, "read")
        self.assertEqual(context.source, "mcp:docs")
        self.assertEqual(context.arguments["path"], "README.md")

    def test_harness_rejects_policy_without_decide(self) -> None:
        class BooleanPolicy:
            """旧式布尔策略替身，用于拒绝缺少 decide 协议的对象。"""

            def is_allowed(self, tool_name: str, arguments: dict[str, Any]) -> bool:
                return True

        with self.assertRaisesRegex(TypeError, "must implement decide"):
            Harness(SequenceProvider(), policy=BooleanPolicy())  # type: ignore[arg-type]


class PolicyExecutorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """验证 Engine 在策略结论与 Executor 之间的调用边界。"""

    async def test_runtime_passes_agent_and_capability_context_to_policy(self) -> None:
        contexts: list[PolicyContext] = []

        class RecordingPolicy:
            """记录评估上下文并允许调用的策略替身。"""

            def decide(self, context: PolicyContext) -> PolicyDecision:
                contexts.append(context)
                return PolicyDecision.allow()

        registered = tool(lambda: "unused", name="read")
        provider = SequenceProvider(
            _assistant_message([ToolUseContent(id="read-context", name="read", arguments={})]),
            _assistant_message([TextContent(text="Read.")]),
        )
        await Harness(provider, policy=RecordingPolicy()).run(
            Agent(name="workspace-reader", instructions="", tools=(registered,)),
            "Read",
        ).result()

        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].agent_name, "workspace-reader")
        self.assertIsNone(contexts[0].mode_name)
        self.assertEqual(contexts[0].capability_type, "tool")
        self.assertEqual(contexts[0].capability_name, "read")
        self.assertEqual(contexts[0].source, "local")

    async def test_allow_executes_once_and_counts_the_requested_call(self) -> None:
        registered = tool(lambda: "unused", name="read")
        provider = SequenceProvider(
            _assistant_message([ToolUseContent(id="read-allow", name="read", arguments={})]),
            _assistant_message([TextContent(text="Read.")]),
        )
        executor = RecordingExecutor()
        result = await Harness(
            provider,
            policy=AllowListPolicy({"read"}),
            executor=executor,
        ).run(
            Agent(name="reader", instructions="", tools=(registered,)),
            "Read the value",
        ).result()

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0][1].id, "read-allow")
        self.assertEqual(result.tool_calls, 1)

    async def test_ask_pauses_until_approved_then_executes_once(self) -> None:
        registered = tool(lambda: "unused", name="publish")

        class AskPolicy:
            """要求确认会影响外部用户的操作。"""

            def decide(self, context: PolicyContext) -> PolicyDecision:
                return PolicyDecision.ask("Publishing affects external users.")

        provider = SequenceProvider(
            _assistant_message(
                [ToolUseContent(id="publish-1", name="publish", arguments={})]
            ),
            _assistant_message([TextContent(text="Published.")]),
        )
        executor = RecordingExecutor()
        run = Harness(
            provider,
            policy=AskPolicy(),
            executor=executor,
        ).run(
            Agent(name="publisher", instructions="", tools=(registered,)),
            "Publish the change",
        )
        events = run.events()
        while True:
            event = await asyncio.wait_for(anext(events), timeout=1)
            if _is_tool_call_completion(event):
                break

        while True:
            approval_event = await asyncio.wait_for(anext(events), timeout=1)
            if isinstance(approval_event, ToolApprovalRequestedEvent):
                break
        self.assertEqual(approval_event.tool_call_id, "publish-1")
        self.assertEqual(approval_event.tool_name, "publish")
        self.assertFalse(run.done)
        self.assertEqual(executor.calls, [])
        self.assertFalse(run.approve_tool("stale-id"))
        self.assertTrue(await asyncio.to_thread(run.approve_tool, "publish-1"))
        self.assertFalse(run.deny_tool("publish-1"))

        result = await run.result()
        remaining = [item async for item in events]
        types = [item.type for item in remaining]

        self.assertEqual(result.final_message.text, "Published.")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(executor.calls[0][1].id, "publish-1")
        approval_resolved = next(
            item
            for item in remaining
            if isinstance(item, ToolApprovalResolvedEvent)
        )
        self.assertEqual(approval_resolved.outcome, "approved")
        self.assertIn("tool_execution_start", types)
        self.assertEqual(provider.requests[1][0][-1].tool_call_id, "publish-1")
        self.assertEqual(provider.requests[1][0][-1].text, "executor-result")

    async def test_approval_denial_is_returned_to_model_without_execution(self) -> None:
        registered = tool(lambda: "unused", name="delete")

        class AskPolicy:
            """要求确认删除记录的操作。"""

            def decide(self, context: PolicyContext) -> PolicyDecision:
                return PolicyDecision.ask("This deletes a record.")

        provider = SequenceProvider(
            _assistant_message(
                [ToolUseContent(id="delete-1", name="delete", arguments={})]
            ),
            _assistant_message([TextContent(text="I will not delete it.")]),
        )
        executor = RecordingExecutor()
        run = Harness(provider, policy=AskPolicy(), executor=executor).run(
            Agent(name="reviewer", instructions="", tools=(registered,)),
            "Delete the record",
        )
        events = run.events()
        while True:
            event = await asyncio.wait_for(anext(events), timeout=1)
            if _is_tool_call_completion(event):
                break

        self.assertTrue(run.deny_tool("delete-1", "User declined this action."))
        result = await run.result()
        remaining = [item async for item in events]
        approval_resolved = next(
            item
            for item in remaining
            if isinstance(item, ToolApprovalResolvedEvent)
        )
        tool_result = provider.requests[1][0][-1]

        self.assertEqual(result.final_message.text, "I will not delete it.")
        self.assertEqual(executor.calls, [])
        self.assertTrue(tool_result.is_error)
        self.assertEqual(tool_result.text, "User declined this action.")
        self.assertEqual(approval_resolved.outcome, "denied")
        self.assertEqual(remaining[-1].type, "agent_settled")

    async def test_cancel_while_waiting_for_approval_commits_tool_pairing(self) -> None:
        registered = tool(lambda: "unused", name="publish")

        class AskPolicy:
            """要求确认未配置自动许可的操作。"""

            def decide(self, context: PolicyContext) -> PolicyDecision:
                return PolicyDecision.ask("Approval required.")

        provider = SequenceProvider(
            _assistant_message(
                [ToolUseContent(id="publish-cancel", name="publish", arguments={})]
            )
        )
        session = Session()
        executor = RecordingExecutor()
        run = Harness(
            provider,
            policy=AskPolicy(),
            executor=executor,
        ).run(
            Agent(name="publisher", instructions="", tools=(registered,)),
            "Publish",
            session=session,
        )
        events = run.events()
        while True:
            event = await asyncio.wait_for(anext(events), timeout=1)
            if _is_tool_call_completion(event):
                break

        self.assertTrue(run.cancel())
        with self.assertRaises(asyncio.CancelledError):
            await run.result()
        remaining = [item async for item in events]
        approval_resolved = next(
            item
            for item in remaining
            if isinstance(item, ToolApprovalResolvedEvent)
        )

        self.assertEqual(executor.calls, [])
        self.assertFalse(session.is_busy)
        result_message = session.messages[-1]
        self.assertEqual(result_message.tool_call_id, "publish-cancel")
        self.assertTrue(result_message.is_error)
        self.assertEqual(approval_resolved.outcome, "cancelled")
        self.assertEqual(remaining[-1].type, "agent_settled")

    async def test_policy_exception_fails_closed_before_executor(self) -> None:
        registered = tool(lambda: "unused", name="write")

        class BrokenPolicy:
            """在决策期间抛错以验证策略错误会阻止工具执行。"""

            def decide(
                self,
                context: PolicyContext,
            ) -> PolicyDecision:
                raise RuntimeError("policy backend unavailable")

        provider = SequenceProvider(
            _assistant_message(
                [ToolUseContent(id="write-1", name="write", arguments={})]
            )
        )
        executor = RecordingExecutor()
        session = Session()
        run = Harness(
            provider,
            policy=BrokenPolicy(),
            executor=executor,
        ).run(
            Agent(name="writer", instructions="", tools=(registered,)),
            "Write",
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "policy backend unavailable"):
            await run.result()

        self.assertEqual(executor.calls, [])
        self.assertTrue(session.messages[-1].is_error)

    async def test_executor_result_must_match_tool_call_id(self) -> None:
        registered = tool(lambda: "unused", name="read")

        class MismatchedExecutor:
            """返回错误调用 ID 的执行器替身。"""

            async def execute(
                self,
                tool: Tool,
                tool_use: ToolUseContent,
            ) -> ToolResultMessage:
                return _tool_result("wrong-id", tool_use.name, "wrong")

        provider = SequenceProvider(
            _assistant_message([ToolUseContent(id="read-1", name="read", arguments={})])
        )
        run = Harness(
            provider,
            policy=AllowListPolicy({"read"}),
            executor=MismatchedExecutor(),
        ).run(
            Agent(name="reader", instructions="", tools=(registered,)),
            "Read",
        )

        with self.assertRaises(ToolExecutorProtocolError):
            await run.result()

    async def test_executor_exception_fails_run_without_retrying_tool(self) -> None:
        registered = tool(lambda: "unused", name="write")

        class BrokenExecutor:
            """记录执行次数并模拟执行器失败。"""

            def __init__(self) -> None:
                self.calls = 0

            async def execute(
                self,
                tool: Tool,
                tool_use: ToolUseContent,
            ) -> ToolResultMessage:
                self.calls += 1
                raise RuntimeError("executor unavailable")

        provider = SequenceProvider(
            _assistant_message([ToolUseContent(id="write-1", name="write", arguments={})])
        )
        executor = BrokenExecutor()
        session = Session()
        run = Harness(
            provider,
            policy=AllowListPolicy({"write"}),
            executor=executor,
        ).run(
            Agent(name="writer", instructions="", tools=(registered,)),
            "Write",
            session=session,
        )

        with self.assertRaisesRegex(RuntimeError, "executor unavailable"):
            await run.result()

        self.assertEqual(executor.calls, 1)
        self.assertTrue(session.messages[-1].is_error)

    async def test_safe_tool_retries_only_explicit_transient_errors(self) -> None:
        registered = tool(lambda: "unused", name="lookup", retry_safe=True)

        class FlakyExecutor:
            """首次报告暂时性失败，第二次返回成功结果。"""

            def __init__(self) -> None:
                self.calls = 0

            async def execute(
                self,
                registered_tool: Tool,
                tool_use: ToolUseContent,
            ) -> ToolResultMessage:
                self.calls += 1
                if self.calls == 1:
                    raise RetryableToolError(
                        "temporary backend failure",
                        retry_after_seconds=0,
                    )
                return _tool_result(tool_use.id, tool_use.name, "recovered")

        executor = FlakyExecutor()
        run = Harness(
            SequenceProvider(
                _assistant_message(
                    [ToolUseContent(id="lookup-1", name="lookup", arguments={})]
                ),
                _assistant_message([TextContent(text="Done.")]),
            ),
            policy=AllowListPolicy({"lookup"}),
            executor=executor,
            tool_retry_settings=ToolRetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        ).run(
            Agent(name="reader", instructions="", tools=(registered,)),
            "Look up the value",
        )

        result = await run.result()
        events = [event async for event in run.events()]
        self.assertEqual(executor.calls, 2)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.tool_attempts, 2)
        self.assertEqual(result.tool_retries, 1)
        retry_start = next(
            event for event in events if isinstance(event, ToolRetryStartEvent)
        )
        retry_end = next(
            event for event in events if isinstance(event, ToolRetryEndEvent)
        )
        self.assertEqual(retry_start.tool_call_id, "lookup-1")
        self.assertEqual(retry_start.error_message, "temporary backend failure")
        self.assertTrue(retry_end.success)
        self.assertEqual(retry_end.attempt, 2)

    async def test_invalid_arguments_are_returned_to_model_for_correction(self) -> None:
        registered = tool(lambda value: value, name="lookup")
        provider = SequenceProvider(
            _assistant_message(
                [
                    ToolUseContent(
                        id="lookup-invalid",
                        name="lookup",
                        arguments={"unexpected": "value"},
                    )
                ]
            ),
            _assistant_message(
                [
                    ToolUseContent(
                        id="lookup-corrected",
                        name="lookup",
                        arguments={"value": "fixed"},
                    )
                ]
            ),
            _assistant_message([TextContent(text="Done.")]),
        )
        executor = RecordingExecutor()
        run = Harness(
            provider,
            policy=AllowListPolicy({"lookup"}),
            executor=executor,
        ).run(
            Agent(name="reader", instructions="", tools=(registered,)),
            "Look up the value",
        )

        await run.result()

        feedback = provider.requests[1][0][-1]
        self.assertTrue(feedback.is_error)
        self.assertEqual(feedback.metadata["error_kind"], "invalid_arguments")
        self.assertIn("This call was not executed", feedback.text)
        self.assertEqual(
            [tool_use.id for _, tool_use in executor.calls],
            ["lookup-corrected"],
        )

    async def test_recoverable_tool_error_is_returned_without_replaying(self) -> None:
        registered = tool(lambda: "unused", name="lookup")

        class RecoverableExecutor:
            """抛出明确可恢复错误并记录执行次数。"""

            def __init__(self) -> None:
                self.calls = 0

            async def execute(
                self,
                registered_tool: Tool,
                tool_use: ToolUseContent,
            ) -> ToolResultMessage:
                self.calls += 1
                raise RecoverableToolError("record was not found " + ("x" * 3000))

        executor = RecoverableExecutor()
        provider = SequenceProvider(
            _assistant_message(
                [ToolUseContent(id="lookup-recoverable", name="lookup", arguments={})]
            ),
            _assistant_message([TextContent(text="No record was found.")]),
        )
        run = Harness(
            provider,
            policy=AllowListPolicy({"lookup"}),
            executor=executor,
        ).run(
            Agent(name="reader", instructions="", tools=(registered,)),
            "Look up the value",
        )

        await run.result()

        feedback = provider.requests[1][0][-1]
        self.assertEqual(executor.calls, 1)
        self.assertTrue(feedback.is_error)
        self.assertEqual(
            feedback.metadata["error_kind"],
            "recoverable_failure",
        )
        self.assertIn("record was not found", feedback.text)
        self.assertIn("side effects may be uncertain", feedback.text)
        self.assertEqual(len(feedback.text), 2000)
        self.assertIn("[Tool feedback truncated.]", feedback.text)

    async def test_exhausted_safe_retries_are_returned_to_model(self) -> None:
        registered = tool(lambda: "unused", name="lookup", retry_safe=True)

        class AlwaysRetryableExecutor:
            """每次执行均报告暂时性失败。"""

            def __init__(self) -> None:
                self.calls = 0

            async def execute(
                self,
                registered_tool: Tool,
                tool_use: ToolUseContent,
            ) -> ToolResultMessage:
                self.calls += 1
                raise RetryableToolError("backend still unavailable")

        executor = AlwaysRetryableExecutor()
        provider = SequenceProvider(
            _assistant_message(
                [ToolUseContent(id="lookup-retries", name="lookup", arguments={})]
            ),
            _assistant_message([TextContent(text="I will try another approach.")]),
        )
        run = Harness(
            provider,
            policy=AllowListPolicy({"lookup"}),
            executor=executor,
            tool_retry_settings=ToolRetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        ).run(
            Agent(name="reader", instructions="", tools=(registered,)),
            "Look up the value",
        )

        await run.result()
        events = [event async for event in run.events()]

        feedback = provider.requests[1][0][-1]
        self.assertEqual(executor.calls, 2)
        self.assertTrue(feedback.is_error)
        self.assertEqual(feedback.metadata["error_kind"], "retry_exhausted")
        self.assertIn("retry limit reached after 2", feedback.text)
        self.assertNotIn("backend still unavailable", feedback.text)
        retry_end = next(
            event for event in events if isinstance(event, ToolRetryEndEvent)
        )
        self.assertFalse(retry_end.success)
        self.assertEqual(retry_end.attempt, 2)

    async def test_unsafe_tool_does_not_retry_even_retryable_error(self) -> None:
        registered = tool(lambda: "unused", name="write")

        class RetryableExecutor:
            """对非幂等工具抛出显式暂时性异常。"""

            def __init__(self) -> None:
                self.calls = 0

            async def execute(
                self,
                registered_tool: Tool,
                tool_use: ToolUseContent,
            ) -> ToolResultMessage:
                self.calls += 1
                raise RetryableToolError("temporary failure")

        executor = RetryableExecutor()
        run = Harness(
            SequenceProvider(
                _assistant_message(
                    [ToolUseContent(id="write-1", name="write", arguments={})]
                )
            ),
            policy=AllowListPolicy({"write"}),
            executor=executor,
            tool_retry_settings=ToolRetrySettings(max_attempts=3),
        ).run(
            Agent(name="writer", instructions="", tools=(registered,)),
            "Write the value",
        )

        with self.assertRaisesRegex(RetryableToolError, "temporary failure"):
            await run.result()
        self.assertEqual(executor.calls, 1)

    async def test_policy_types_are_accepted_by_static_contract(self) -> None:
        policy: PolicyEngine = AllowListPolicy({"read"})
        self.assertEqual(policy.decide(policy_context("read")).outcome, "allow")


class LocalExecutorTests(unittest.IsolatedAsyncioTestCase):
    """验证默认本地 Executor 的结果规范化和调用 ID 配对。"""

    async def test_local_executor_serializes_non_string_result(self) -> None:
        registered = tool(lambda: {"ok": True}, name="status")
        result = await LocalToolExecutor().execute(
            registered,
            ToolUseContent(id="status-1", name="status", arguments={}),
        )

        self.assertEqual(result.tool_call_id, "status-1")
        self.assertEqual(result.text, '{"ok": true}')
        self.assertFalse(result.is_error)
