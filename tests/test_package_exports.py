"""验证共享类型、组件协议和通用工具通过包入口公开。"""

from contextlib import redirect_stdout
from io import StringIO
import unittest

from sec.apps.cli import main


class PackageExportTests(unittest.TestCase):
    """确保有实现的子包从包入口公开稳定符号。"""

    def test_sdk_entrypoint_hides_agent_assembly_internals(self) -> None:
        import sec
        import sec.apis as apis

        self.assertTrue(hasattr(apis, "AsyncSecClient"))
        self.assertTrue(hasattr(apis, "SecClient"))
        self.assertTrue(hasattr(apis, "Session"))
        self.assertTrue(hasattr(apis, "Run"))
        for name in ("Agent", "Harness", "create_agent", "create_harness"):
            self.assertFalse(hasattr(apis, name))
            self.assertFalse(hasattr(sec, name))

    def test_tools_package_exports_builtin_tool_factory(self) -> None:
        from sec.tools import create_builtin_tools
        from sec.tools._builtins import create_builtin_tools as builtins_factory

        self.assertIs(create_builtin_tools, builtins_factory)

    def test_sdk_entrypoint_exports_shared_data_contracts(self) -> None:
        from importlib.util import find_spec

        import sec.apis as apis

        self.assertIsNone(find_spec("sec.types"))
        expected = {
            "AgentMessage",
            "AssistantMessage",
            "BashExecutionMessage",
            "BranchSummaryMessage",
            "CompactionSummaryMessage",
            "Content",
            "content_from_dict",
            "content_to_dict",
            "CustomMessage",
            "ContextUsage",
            "FileContent",
            "Limits",
            "Message",
            "merge_text",
            "message_from_dict",
            "message_to_dict",
            "ModelResponseCompleted",
            "ModelStreamEvent",
            "ModelTextDelta",
            "RunUsage",
            "PolicyCapabilityType",
            "PolicyContext",
            "PolicyDecision",
            "PolicyOutcome",
            "SessionEvent",
            "RunResult",
            "SessionEntry",
            "SessionSnapshot",
            "SessionState",
            "SessionStats",
            "SessionTokens",
            "StoredObject",
            "SystemMessage",
            "TextContent",
            "ThinkingContent",
            "ToolResultContent",
            "ToolResultMessage",
            "ToolUseContent",
            "UserMessage",
            "validate_timeout",
        }
        self.assertTrue(expected.issubset(set(apis.__all__)))
        self.assertTrue(all(hasattr(apis, name) for name in expected))

    def test_sdk_entrypoint_exports_subscription_overflow_error(self) -> None:
        import sec
        import sec.apis as apis
        from sec.exceptions import SecSubscriptionOverflow, SecUIHandlerError

        self.assertIs(apis.SecSubscriptionOverflow, SecSubscriptionOverflow)
        self.assertIs(sec.SecSubscriptionOverflow, SecSubscriptionOverflow)
        self.assertIs(apis.SecUIHandlerError, SecUIHandlerError)
        self.assertIs(sec.SecUIHandlerError, SecUIHandlerError)

    def test_session_events_are_dataclass_instances(self) -> None:
        from dataclasses import is_dataclass

        from sec._types.events import (
            AgentSettledEvent,
            CompactionStartEvent,
            ToolApprovalRequestedEvent,
            ToolRetryStartEvent,
        )

        events = (
            AgentSettledEvent(status="completed"),
            CompactionStartEvent(reason="threshold"),
            ToolApprovalRequestedEvent(
                tool_call_id="call-1",
                tool_name="publish",
                args={},
            ),
            ToolRetryStartEvent(
                tool_call_id="call-1",
                tool_name="publish",
                attempt=1,
                max_attempts=2,
                delay_ms=0,
                error_message="temporary failure",
            ),
        )
        self.assertTrue(all(is_dataclass(event) for event in events))
        self.assertEqual(
            [event.type for event in events],
            [
                "agent_settled",
                "compaction_start",
                "tool_approval_requested",
                "tool_retry_start",
            ],
        )

    def test_sdk_model_types_use_dataclasses_and_pi_wire_codec(self) -> None:
        from dataclasses import is_dataclass

        from sec._types import Model, ModelCostTier, model_from_dict, model_to_dict

        raw = {
            "id": "model-1",
            "name": "Model One",
            "api": "openai-completions",
            "provider": "provider",
            "baseUrl": "https://example.invalid",
            "reasoning": True,
            "thinkingLevelMap": {"high": "high", "max": None},
            "input": ["text", "image"],
            "inputLimits": {
                "maxRequestBytes": 1000,
                "images": {
                    "resize": {"maxWidth": 1200, "jpegQuality": 85},
                    "maxPerMessage": 4,
                    "maxPerRequest": 8,
                },
            },
            "cost": {
                "input": 1,
                "output": 2,
                "cacheRead": 0.5,
                "cacheWrite": 0.75,
                "tiers": [
                    {
                        "input": 0.8,
                        "output": 1.6,
                        "cacheRead": 0.4,
                        "cacheWrite": 0.6,
                        "inputTokensAbove": 100000,
                    }
                ],
            },
            "contextWindow": 128000,
            "maxTokens": 16000,
            "samplingParams": {"temperature": 0.2},
            "headers": {"x-provider": "value"},
            "compat": {"supportsDeveloperRole": True},
            "promptCache": {"short": 300, "long": 3600},
        }

        model = model_from_dict(raw)
        self.assertIsInstance(model, Model)
        self.assertTrue(is_dataclass(model))
        self.assertEqual(model.base_url, raw["baseUrl"])
        self.assertEqual(model.cost.tiers, [
            ModelCostTier(
                input=0.8,
                output=1.6,
                cache_read=0.4,
                cache_write=0.6,
                input_tokens_above=100000,
            )
        ])
        self.assertEqual(model_to_dict(model), raw)
        with self.assertRaisesRegex(ValueError, "reasoning"):
            model_from_dict({**raw, "reasoning": "yes"})

        minimal = {
            "id": "minimal",
            "name": "Minimal",
            "api": "api",
            "provider": "provider",
            "baseUrl": "https://example.invalid",
            "reasoning": False,
            "input": ["text"],
            "cost": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
            },
            "contextWindow": 1000,
            "maxTokens": 100,
        }
        minimal_model = model_from_dict(minimal)
        self.assertIsNone(minimal_model.prompt_cache)
        self.assertEqual(model_to_dict(minimal_model), minimal)

    def test_adapter_and_application_types_are_not_core_type_exports(self) -> None:
        import sec._types as core_types
        import sec._types.events as event_types
        import sec.apps as apps

        from sec.apps.extension_ui import ExtensionUIRequest
        from sec._types.events import CompactionResult, QueueState

        self.assertFalse(hasattr(core_types, "BashResult"))
        self.assertFalse(hasattr(core_types, "RpcCommand"))
        self.assertFalse(hasattr(core_types, "ExtensionUIRequest"))
        self.assertIs(apps.ExtensionUIRequest, ExtensionUIRequest)
        self.assertIs(event_types.CompactionResult, CompactionResult)
        self.assertIs(event_types.QueueState, QueueState)
        self.assertTrue(hasattr(core_types, "SessionSnapshot"))

    def test_session_statistics_and_run_results_have_distinct_semantics(self) -> None:
        from dataclasses import fields, is_dataclass

        from sec._types import SessionState, SessionStats, SessionTokens
        from sec._types.results import RunResult, RunUsage

        self.assertTrue(
            all(
                is_dataclass(item)
                for item in (SessionState, SessionStats, SessionTokens)
            )
        )
        self.assertTrue(all(is_dataclass(item) for item in (RunUsage, RunResult)))
        self.assertEqual(
            {descriptor.name for descriptor in fields(SessionStats)},
            {
                "session_id",
                "user_messages",
                "assistant_messages",
                "tool_calls",
                "tool_results",
                "total_messages",
                "tokens",
                "cost",
                "session_file",
                "context_usage",
            },
        )
        import sec._types as canonical_types

        self.assertFalse(hasattr(canonical_types, "RunResult"))

    def test_sdk_entrypoint_exports_component_contracts(self) -> None:
        from importlib.util import find_spec

        import sec
        import sec.apis as apis

        expected = {
            "ManagedStreamingModelProvider",
            "ModelProvider",
            "ObjectStore",
            "PolicyEngine",
            "SessionStoreProtocol",
            "StreamingModelProvider",
            "ToolExecutor",
            "TokenEstimator",
        }
        self.assertTrue(all(hasattr(apis, name) for name in expected))
        self.assertTrue(all(hasattr(sec, name) for name in expected))
        self.assertIsNone(find_spec("sec.protocols"))

    def test_utils_package_exports_shared_lock(self) -> None:
        from sec.utils import AsyncRWLock

        import sec.utils as utils

        self.assertEqual(utils.__all__, ["AsyncRWLock"])
        self.assertIs(utils.AsyncRWLock, AsyncRWLock)

    def test_cli_package_exports_startup_entrypoint(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(("/help",))
        self.assertEqual(exit_code, 0)
        self.assertIn("/help", output.getvalue())

    def test_cli_without_command_starts_interactive_chat(self) -> None:
        from unittest.mock import patch

        async def chat(*_args):
            return 0

        def run(coroutine):
            coroutine.close()
            return 0

        with (
            patch("sec.apps.cli.app._chat", side_effect=chat) as chat_entry,
            patch("sec.apps.cli.app.asyncio.run", side_effect=run),
        ):
            self.assertEqual(
                main(("--session", "session-1", "--extension", "sample")),
                0,
            )
        chat_entry.assert_called_once_with("session-1", ("sample",), "normal")

    def test_cli_slash_commands_are_registered_and_dispatched(self) -> None:
        from sec.apps.cli import SlashCommand, SlashCommandRegistry

        registry = SlashCommandRegistry()
        received: list[tuple[str, ...]] = []
        registry.register(
            SlashCommand(
                "echo",
                "Echo command arguments.",
                lambda args: received.append(args) or 0,
            )
        )
        self.assertEqual(registry.dispatch(("/echo", "hello", "world")), 0)
        self.assertEqual(received, [("hello", "world")])

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(SlashCommand("echo", "Duplicate.", lambda args: 0))
        with self.assertRaisesRegex(ValueError, "unknown slash command"):
            registry.dispatch(("/missing",))

    def test_cli_main_dispatches_help_and_reports_unknown_commands(self) -> None:
        from contextlib import redirect_stderr

        error_output = StringIO()
        help_output = StringIO()
        with redirect_stdout(help_output):
            self.assertEqual(main(("/help",)), 0)
        self.assertIn("/help", help_output.getvalue())

        with redirect_stderr(error_output):
            self.assertEqual(main(("/missing",)), 2)
        self.assertIn("unknown slash command", error_output.getvalue())
