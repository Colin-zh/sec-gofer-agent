from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
import tempfile
import time
import unittest

from sec.apps.desktop.trace_store import TraceRecorder, TraceStore
from sec import AllowListPolicy, RetrySettings, tool
from sec.agent import Agent
from sec._runtime.harness import Harness
from sec._types.content import TextContent, ThinkingContent, ToolUseContent
from sec._types.events import (
    AssistantDoneEvent,
    EmptySessionEvent,
    MessageBoundaryEvent,
    MessageUpdateEvent,
    ModelRequestStartEvent,
    ModelRequestEndEvent,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from sec._types.messages import AssistantMessage, Message, UserMessage, Usage, UsageCost
from sec._types.streaming import ModelResponseCompleted, ModelStreamEvent, ModelTextDelta
from sec.exceptions import RetryableProviderError
from sec.tools.tool import ToolDefinition


class TraceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".sec" / "desktop-traces.sqlite3"
        self.store = TraceStore(self.path)

    def test_trace_persists_event_tree_and_redacts_bounded_tool_payloads(self) -> None:
        recorder = TraceRecorder(self.store, "run-1234", "session-5678")
        recorder.record(EmptySessionEvent("turn_start"))
        recorder.record(
            MessageBoundaryEvent(
                "message_start",
                UserMessage(
                    content="this chat input must not be copied",
                    timestamp=time.time(),
                    metadata={"turn_id": "turn-1"},
                ),
            )
        )
        recorder.record(
            ToolExecutionStartEvent(
                "call-1",
                "read_file",
                {
                    "path": "src/app.py",
                    "api_key": "must-not-persist",
                    "options": {"password": "also-secret"},
                },
            )
        )
        recorder.record(
            ToolApprovalRequestedEvent(
                "call-1",
                "read_file",
                {"path": "src/app.py"},
                reason="Review tool access",
            )
        )
        time.sleep(0.01)
        recorder.record(
            ToolApprovalResolvedEvent("call-1", "read_file", "approved"),
        )
        recorder.record(
            ToolExecutionEndEvent(
                "call-1",
                "read_file",
                "safe output",
                False,
            )
        )
        recorder.finish("completed")

        root = self.store.get_tree("run-1234")[0]
        turn = root["children"][0]
        tool = turn["children"][0]
        approval = tool["children"][0]
        self.assertEqual(root["kind"], "run")
        self.assertEqual(turn["kind"], "turn")
        self.assertEqual(tool["kind"], "tool")
        self.assertEqual(approval["kind"], "approval")
        self.assertGreater(approval["approval_wait_ms"], 0)
        self.assertGreaterEqual(tool["elapsed_ms"], 0)
        self.assertNotEqual(approval["approval_wait_ms"], tool["elapsed_ms"])
        self.assertEqual(tool["details"]["input"]["api_key"], "[REDACTED]")
        self.assertEqual(tool["details"]["input"]["options"]["password"], "[REDACTED]")
        self.assertEqual(tool["details"]["output"], "safe output")
        self.assertEqual(
            self.store.get_run("run-1234")["status"],
            "completed",
        )

        reopened = TraceStore(self.path)
        self.assertEqual(reopened.list_runs()[0]["run_id"], "run-1234")
        events = reopened.get_node_events("run-1234", tool["node_id"])
        self.assertIn("tool_execution_start", [event.event_type for event in events])
        self.assertNotIn(
            "must-not-persist",
            str([event.payload for event in events]),
        )

    def test_model_node_uses_provider_usage_without_copying_chat_or_thinking(self) -> None:
        recorder = TraceRecorder(self.store, "run-model", "session-model")
        recorder.record(EmptySessionEvent("turn_start"))
        usage = Usage(
            input=12,
            output=7,
            cache_read=3,
            cache_write=0,
            total_tokens=19,
            cost=UsageCost(input=0.01, output=0.02, cache_read=0, cache_write=0, total=0.03),
        )
        message = AssistantMessage(
            content=[
                TextContent(text="chat response not copied"),
                ThinkingContent(thinking="private reasoning not copied"),
            ],
            api="messages",
            provider="anthropic",
            model="claude-test",
            usage=usage,
            stop_reason="stop",
            timestamp=time.time(),
            metadata={"turn_id": "turn-model"},
        )
        recorder.record(MessageBoundaryEvent("message_start", message))
        recorder.record(
            MessageUpdateEvent(
                AssistantDoneEvent(reason="stop", message=message),
                usage=usage,
            )
        )
        recorder.record(MessageBoundaryEvent("message_end", message))
        recorder.finish("completed")

        model = self.store.get_tree("run-model")[0]["children"][0]["children"][0]
        self.assertEqual(model["kind"], "model")
        self.assertEqual(model["details"]["provider"], "anthropic")
        self.assertEqual(model["details"]["usage"]["input"], 12)
        self.assertEqual(model["details"]["usage"]["output"], 7)
        self.assertEqual(model["details"]["usage"]["cost"], 0.03)
        self.assertIsNone(model["elapsed_ms"])
        stored = str(model)
        self.assertNotIn("chat response not copied", stored)
        self.assertNotIn("private reasoning not copied", stored)
        self.assertIn("未记录", model["details"]["input"])
        self.assertIn("未记录", model["details"]["output"])

    def test_model_attempt_records_input_output_timing_without_duplicate_messages(self) -> None:
        recorder = TraceRecorder(self.store, "run-observed", "session-observed")
        recorder.record(ModelRequestStartEvent(
            request_id="request-1", turn_id="turn-1", attempt=1,
            purpose="response", started_at=100,
            input={
                "messages": [
                    {"role": "system", "content": "Use tools."},
                    {"role": "user", "content": "Read the report."},
                ],
                "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
            },
        ))
        model = self.store.get_tree(recorder.run_id)[0]["children"][0]["children"][0]
        self.assertEqual(model["status"], "running")
        self.assertIsNone(model["elapsed_ms"])
        self.assertEqual(model["started_at"], 100)
        self.assertEqual(model["details"]["input"]["messages"][1]["content"], "Read the report.")
        response = AssistantMessage(
            content=[TextContent("Here is the report."), ThinkingContent("omit this")],
            api="messages", provider="anthropic", model="example",
            usage=Usage(12, 7, 0, 0, 19, UsageCost(0, 0, 0, 0, 0.03)),
            stop_reason="stop", timestamp=102,
            response_id="response-1", metadata={"turn_id": "turn-1"},
        )
        recorder.record(ModelRequestEndEvent(
            request_id="request-1", turn_id="turn-1", attempt=1, purpose="response",
            ended_at=102, elapsed_ms=1250.25, status="completed", response=response,
        ))
        recorder.record(MessageBoundaryEvent("message_start", response))
        recorder.record(MessageUpdateEvent(
            AssistantDoneEvent("stop", response), usage=response.usage,
        ))
        recorder.record(MessageBoundaryEvent("message_end", response))
        recorder.finish("completed")
        tree = TraceStore(self.path).get_tree(recorder.run_id)
        models = tree[0]["children"][0]["children"]
        self.assertEqual(len(models), 1)
        model = models[0]
        self.assertEqual(model["status"], "completed")
        self.assertEqual(model["started_at"], 100)
        self.assertEqual(model["ended_at"], 102)
        self.assertEqual(model["elapsed_ms"], 1250.25)
        self.assertEqual(model["details"]["usage"]["cost"], 0.03)
        self.assertEqual(model["details"]["response_id"], "response-1")
        self.assertEqual(model["details"]["output"]["content"][0]["text"], "Here is the report.")
        self.assertNotIn("omit this", str(model))
        events = self.store.get_node_events(recorder.run_id, model["node_id"])
        self.assertEqual([event.event_type for event in events], [
            "model_request_start", "model_request_end", "message_start",
            "message_update", "message_end",
        ])

    def test_failed_cancelled_and_compaction_attempts_keep_distinct_outcomes(self) -> None:
        recorder = TraceRecorder(self.store, "run-attempts", "session-attempts")
        for attempt, status, error in (
            (1, "failed", "password=must-not-persist"),
            (2, "cancelled", None),
        ):
            recorder.record(ModelRequestStartEvent(
                request_id="request-retry", turn_id="turn-1", attempt=attempt,
                purpose="compaction", started_at=100 + attempt, input={"messages": []},
            ))
            recorder.record(ModelRequestEndEvent(
                request_id="request-retry", turn_id="turn-1", attempt=attempt,
                purpose="compaction", ended_at=110 + attempt, elapsed_ms=20,
                status=status, error=error,
            ))
        recorder.finish("cancelled")
        models = self.store.get_tree(recorder.run_id)[0]["children"][0]["children"]
        self.assertEqual([model["status"] for model in models], ["failed", "cancelled"])
        self.assertTrue(all(model["details"]["purpose"] == "compaction" for model in models))
        self.assertTrue(all(model["elapsed_ms"] == 20 for model in models))
        self.assertNotIn("must-not-persist", str(models))
        self.assertEqual(len({model["node_id"] for model in models}), 2)

    def test_model_payloads_redact_secrets_and_omit_reasoning_and_binary_data(self) -> None:
        recorder = TraceRecorder(self.store, "run-redacted", "session-redacted")
        recorder.record(ModelRequestStartEvent(
            request_id="request-private", turn_id="turn-1", attempt=1,
            purpose="response", started_at=100,
            input={"messages": [{
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "hello", "text_signature": "signature-private"},
                    {"type": "thinking", "thinking": "private-reasoning"},
                    {"type": "file", "url": "data:private-file", "data": "private-binary",
                     "data_type": "image/png"},
                    {"type": "tool_use", "arguments": {"api_key": "private-key"}},
                ],
            }]},
        ))
        model = self.store.get_tree(recorder.run_id)[0]["children"][0]["children"][0]
        stored = str(model)
        for secret in (
            "signature-private", "private-reasoning", "private-file",
            "private-binary", "private-key",
        ):
            self.assertNotIn(secret, stored)
        self.assertIn("[REDACTED]", stored)
        self.assertIn("hello", stored)

    def test_large_model_payload_does_not_discard_request_identity_or_output(self) -> None:
        recorder = TraceRecorder(self.store, "run-large", "session-large")
        request = {"messages": [{"role": "user", "content": str(i) * 5000}
                                for i in range(20)]}
        recorder.record(ModelRequestStartEvent(
            request_id="request-large", turn_id="turn-1", attempt=1,
            purpose="response", started_at=100, input=request,
        ))
        response = AssistantMessage(
            content=[TextContent("finished")], api="test", provider="test", model="test",
            usage=Usage(0, 0, 0, 0, 0, UsageCost(0, 0, 0, 0, 0)),
            stop_reason="stop", timestamp=101, metadata={"usage_reported": False},
        )
        recorder.record(ModelRequestEndEvent(
            request_id="request-large", turn_id="turn-1", attempt=1,
            purpose="response", ended_at=101, elapsed_ms=1000,
            status="completed", response=response,
        ))
        model = self.store.get_tree(recorder.run_id)[0]["children"][0]["children"][0]
        self.assertEqual(model["details"]["request_id"], "request-large")
        self.assertTrue(model["details"]["input"]["_trace_truncated"])
        self.assertIsInstance(model["details"]["input"]["messages"], list)
        self.assertNotIn("preview", model["details"]["input"])
        self.assertEqual(model["details"]["output"]["content"][0]["text"], "finished")
        self.assertIsNone(model["details"]["usage"])
        self.assertIsNone(model["details"]["output"]["usage"])
        with self.store._connect() as connection:
            encoded = connection.execute(
                "SELECT details_json FROM trace_nodes WHERE node_id = ?",
                (model["node_id"],),
            ).fetchone()[0]
        self.assertLessEqual(len(encoded.encode("utf-8")), 16384)

    def test_fragmented_history_text_is_merged_before_trace_limits(self) -> None:
        from sec.apps.desktop.trace_store import _model_trace_payload

        fragments = [{"type": "text", "text": f"{i} "} for i in range(150)]
        payload = _model_trace_payload({"messages": [{
            "role": "assistant", "content": [
                *fragments,
                {"type": "tool_use", "id": "call-1", "name": "lookup", "arguments": {}},
                {"type": "text", "text": "after"},
            ],
        }]})
        content = payload["messages"][0]["content"]
        self.assertEqual(len(content), 3)
        self.assertEqual(content[0]["text"], "".join(f"{i} " for i in range(150)))
        self.assertEqual(content[1]["type"], "tool_use")
        self.assertEqual(content[2]["text"], "after")

    def test_structural_truncation_is_valid_bounded_redacted_json(self) -> None:
        import json
        from sec.apps.desktop.trace_store import _model_trace_payload

        for text in ('测"试\\\n' * 5000, "plain" * 5000, "short"):
            with self.subTest(text_length=len(text)):
                payload = _model_trace_payload({
                    "messages": [
                        {"role": "user", "content": text, "api_key": "never-persist"}
                        for _ in range(30)
                    ],
                    "tools": [{"name": "lookup", "description": text}],
                })
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                self.assertLessEqual(len(encoded.encode("utf-8")), 16384 // 3)
                self.assertNotIn("never-persist", encoded)
                self.assertIsInstance(payload["messages"], list)
                self.assertIsInstance(payload["tools"], list)
                self.assertNotIn("preview", payload)
                self.assertEqual(json.loads(encoded), payload)
        payload = _model_trace_payload({"messages": [{
            "role": "user", "content": "a" * 8000,
        }]})
        self.assertIn("[TRUNCATED]", payload["messages"][0]["content"])
        payload = _model_trace_payload({"messages": [
            {"role": "user", "content": f"message-{i} " + "x" * 4000}
            for i in range(30)
        ]})
        messages = payload["messages"]
        self.assertTrue(messages[0]["content"].startswith("message-0 "))
        self.assertTrue(messages[-1]["content"].startswith("message-29 "))
        self.assertGreater(messages[1]["omitted_items"], 0)
        self.assertNotIn("preview", payload)

    def test_structural_budget_handles_nested_arrays_and_escaped_unicode(self) -> None:
        import json
        from sec.apps.desktop.trace_store import _bounded_structure

        values = (
            [{"nested": [["测\n\"\\" * 1000] * 10]}] * 10,
            {"long": "测\n\"\\" * 1000, "list": list(range(100))},
            [{"field": {"inner": "x" * 10000}}] * 100,
        )
        for budget in (64, 128, 256, 1024, 5461):
            for value in values:
                with self.subTest(budget=budget, kind=type(value).__name__):
                    encoded = json.dumps(
                        _bounded_structure(value, budget),
                        ensure_ascii=False, separators=(",", ":"),
                    )
                    self.assertLessEqual(len(encoded.encode("utf-8")), budget)
                    self.assertIsInstance(json.loads(encoded), (dict, list))


class ModelTraceRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_retry_tool_and_response_persist_as_actual_attempts(self) -> None:
        @tool
        def lookup() -> str:
            """Return a local test result."""
            return "local result"

        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            async def stream(
                self, messages: Sequence[Message], tools: Sequence[ToolDefinition],
            ) -> AsyncIterator[ModelStreamEvent]:
                self.calls += 1
                if self.calls == 1:
                    raise RetryableProviderError("temporary failure")
                content: list[TextContent | ToolUseContent] = (
                    [ToolUseContent(id="call-1", name="lookup", arguments={})]
                    if self.calls == 2 else [TextContent("final answer")]
                )
                if self.calls == 3:
                    yield ModelTextDelta("final answer")
                yield ModelResponseCompleted(AssistantMessage(
                    content=content, api="test", provider="test", model="test",
                    usage=Usage(8, 3, 0, 0, 11, UsageCost(0, 0, 0, 0, 0)),
                    stop_reason="tool_use" if self.calls == 2 else "stop",
                    timestamp=time.time(), metadata={"usage_reported": True},
                ))

        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.sqlite3")
            run = Harness(
                Provider(), policy=AllowListPolicy({"lookup"}),
                retry_settings=RetrySettings(
                    max_attempts=2, base_delay_seconds=0, max_delay_seconds=0,
                ),
            ).run(Agent(name="trace", instructions="Use lookup.", tools=(lookup,)), "question")
            recorder = TraceRecorder(store, run.id, run.session.id)
            async for event in run.events():
                recorder.record(event)
            result = await run.result()
            recorder.finish("completed")
            root = TraceStore(store.path).get_tree(run.id)[0]
        children = root["children"][0]["children"]
        models = [node for node in children if node["kind"] == "model"]
        tools = [node for node in children if node["kind"] == "tool"]
        self.assertEqual(result.text, "final answer")
        self.assertEqual(len(models), 3)
        self.assertEqual(len(tools), 1)
        self.assertEqual([node["status"] for node in models], [
            "failed", "completed", "completed",
        ])
        self.assertEqual([node["details"]["attempt"] for node in models], [1, 2, 1])
        self.assertEqual(
            models[0]["details"]["request_id"], models[1]["details"]["request_id"],
        )
        self.assertNotEqual(
            models[1]["details"]["request_id"], models[2]["details"]["request_id"],
        )
        self.assertEqual(
            models[2]["details"]["input"]["messages"][-1]["role"], "tool_result",
        )
        self.assertEqual(
            models[2]["details"]["output"]["content"][0]["text"], "final answer",
        )
        self.assertEqual(
            models[1]["details"]["output"]["content"][0]["name"], "lookup",
        )
        self.assertTrue(all(node["elapsed_ms"] >= 0 for node in models))
        self.assertTrue(all(node["status"] != "running" for node in children))


if __name__ == "__main__":
    unittest.main()
