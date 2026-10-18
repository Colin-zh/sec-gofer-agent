"""验证 sec 配置加载及 OpenAI、Anthropic Provider 的消息适配。

测试使用假的异步 SDK 客户端，不读取真实 .env 凭据，也不访问外部网络。
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from anthropic import APIConnectionError as AnthropicAPIConnectionError
from openai import APIConnectionError as OpenAIAPIConnectionError
from sqlalchemy.engine import URL

from sec._types.content import TextContent
from sec._types.messages import Message, SystemMessage, UserMessage
from sec import (
    Config,
    ConfigError,
    RetrySettings,
    RuntimeSettings,
    StorageSettings,
    ToolRetrySettings,
)
from sec.agent import Agent
from sec.apis.client import create_agent, create_database, create_harness
from sec.config import ProviderSettings
from sec.exceptions import ProviderProtocolError, RetryableProviderError
from sec.providers.anthropic import AnthropicProvider
from sec.providers._errors import retryable_status_error
from sec.providers.openai import OpenAIProvider
from sec.tools.tool import ToolDefinition
from sec._types.streaming import ModelResponseCompleted, ModelTextDelta


def _user_message(text: str) -> UserMessage:
    return UserMessage([TextContent(text)], 1.0)


def text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=text, tool_calls=None)


def tool_delta(
    *,
    index: int,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def openai_chunk(
    delta: SimpleNamespace | None = None,
    *,
    finish_reason: str | None = None,
    usage: SimpleNamespace | None = None,
    has_choice: bool = True,
) -> SimpleNamespace:
    choices = (
        [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
        if has_choice
        else []
    )
    return SimpleNamespace(choices=choices, usage=usage)


class FakeOpenAIStream:
    """提供异步分片，并记录 SDK 流关闭情况。"""

    def __init__(self, chunks: Sequence[SimpleNamespace]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> FakeOpenAIStream:
        return self

    async def __anext__(self) -> SimpleNamespace:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


class FakeAnthropicStream:
    """模拟 Anthropic 流上下文、文本增量和最终消息。"""

    def __init__(self, texts: Sequence[str], final_message: SimpleNamespace) -> None:
        self.text_stream = self._iterate(texts)
        self._final_message = final_message
        self.closed = False

    async def _iterate(self, texts: Sequence[str]) -> AsyncIterator[str]:
        for text in texts:
            yield text

    async def __aenter__(self) -> FakeAnthropicStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def get_final_message(self) -> SimpleNamespace:
        return self._final_message


class ConfigTests(unittest.TestCase):
    """验证配置来源优先级、分层默认值及错误处理。"""

    def test_environment_overrides_dotenv_and_hides_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "SEC_PROVIDER=openai\n"
                "SEC_MODEL=file-model\n"
                "OPENAI_API_KEY=file-secret\n"
                "SEC_EVENT_BUFFER_SIZE=12\n"
                "SEC_SESSION_STORE_BACKEND=database\n"
                "SEC_DATABASE_URL=sqlite+aiosqlite:///from-file.db\n",
                encoding="utf-8",
            )
            config = Config.from_env(
                env_file,
                environ={
                    "OPENAI_MODEL": "environment-model",
                    "OPENAI_API_KEY": "environment-secret",
                    "SEC_AGENT_MAX_MODEL_CALLS": "5",
                    "SEC_MODEL_MAX_ATTEMPTS": "4",
                    "SEC_MODEL_RETRY_BASE_DELAY_SECONDS": "0.25",
                    "SEC_MODEL_RETRY_MAX_DELAY_SECONDS": "3",
                    "SEC_TOOL_MAX_ATTEMPTS": "4",
                    "SEC_TOOL_RETRY_BASE_DELAY_SECONDS": "0.1",
                    "SEC_TOOL_RETRY_MAX_DELAY_SECONDS": "2",
                    "SEC_SESSION_STORE_BACKEND": "database",
                    "SEC_DATABASE_URL": "sqlite+aiosqlite:///from-environment.db",
                },
            )

        self.assertEqual(config.provider.model, "environment-model")
        self.assertEqual(config.provider.api_key, "environment-secret")
        self.assertEqual(config.agent.max_model_calls, 5)
        self.assertEqual(config.agent.max_tool_calls, 32)
        self.assertEqual(config.runtime.event_buffer_size, 12)
        self.assertEqual(config.retry.max_attempts, 4)
        self.assertEqual(config.retry.base_delay_seconds, 0.25)
        self.assertEqual(config.retry.max_delay_seconds, 3)
        self.assertEqual(config.tool_retry.max_attempts, 4)
        self.assertEqual(config.tool_retry.base_delay_seconds, 0.1)
        self.assertEqual(config.tool_retry.max_delay_seconds, 2)
        self.assertEqual(config.storage.session_store_backend, "database")
        self.assertEqual(
            config.storage.database_url,
            "sqlite+aiosqlite:///from-environment.db",
        )
        self.assertNotIn("environment-secret", repr(config))
        self.assertNotIn("file-secret", repr(config))
        self.assertNotIn("from-environment.db", repr(config))

    def test_split_database_credentials_quote_special_characters(self) -> None:
        password = "p@ss:/#%word"
        config = Config.from_env(
            None,
            environ={
                "SEC_PROVIDER": "openai",
                "SEC_MODEL": "test-model",
                "OPENAI_API_KEY": "test-key",
                "SEC_SESSION_STORE_BACKEND": "database",
                "SEC_DATABASE_DRIVER": "opengauss+asyncpg",
                "SEC_DATABASE_HOST": "db.example.invalid",
                "SEC_DATABASE_PORT": "5432",
                "SEC_DATABASE_USERNAME": "sec-user",
                "SEC_DATABASE_PASSWORD": password,
                "SEC_DATABASE_NAME": "sec",
            },
        )

        url = config.storage.sqlalchemy_url
        if not isinstance(url, URL):
            self.fail("split database settings should produce a SQLAlchemy URL")
        self.assertEqual(url.drivername, "opengauss+asyncpg")
        self.assertEqual(url.password, password)
        self.assertEqual(url.host, "db.example.invalid")
        self.assertEqual(url.port, 5432)
        self.assertEqual(url.database, "sec")
        self.assertIn("%40", url.render_as_string(hide_password=False))
        self.assertNotIn(password, repr(config))

    def test_dotenv_split_password_keeps_raw_at_sign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "SEC_PROVIDER=openai\n"
                "SEC_MODEL=test-model\n"
                "OPENAI_API_KEY=test-key\n"
                "SEC_SESSION_STORE_BACKEND=database\n"
                "SEC_DATABASE_DRIVER=opengauss+asyncpg\n"
                "SEC_DATABASE_HOST=db.example.invalid\n"
                "SEC_DATABASE_PORT=5432\n"
                "SEC_DATABASE_USERNAME=sec-user\n"
                "SEC_DATABASE_PASSWORD='p@ss#word'\n"
                "SEC_DATABASE_NAME=sec\n",
                encoding="utf-8",
            )
            config = Config.from_env(env_file, environ={})

        url = config.storage.sqlalchemy_url
        if not isinstance(url, URL):
            self.fail("split database settings should produce a SQLAlchemy URL")
        self.assertEqual(url.password, "p@ss#word")

    def test_split_database_settings_must_be_complete_and_exclusive(self) -> None:
        with self.assertRaisesRegex(ConfigError, "SEC_DATABASE_PORT"):
            Config.from_env(
                None,
                environ={
                    "SEC_PROVIDER": "openai",
                    "SEC_MODEL": "test-model",
                    "OPENAI_API_KEY": "test-key",
                    "SEC_SESSION_STORE_BACKEND": "database",
                    "SEC_DATABASE_DRIVER": "opengauss+asyncpg",
                    "SEC_DATABASE_HOST": "db.example.invalid",
                    "SEC_DATABASE_USERNAME": "sec-user",
                    "SEC_DATABASE_PASSWORD": "secret",
                    "SEC_DATABASE_NAME": "sec",
                },
            )

        with self.assertRaisesRegex(ConfigError, "either SEC_DATABASE_URL"):
            StorageSettings(
                session_store_backend="database",
                database_url="sqlite+aiosqlite:///sec.db",
                database_driver="opengauss+asyncpg",
                database_host="db.example.invalid",
                database_port=5432,
                database_username="sec-user",
                database_password="secret",
                database_name="sec",
            )

    def test_database_schema_is_applied_as_connection_search_path(self) -> None:
        config = Config.from_env(
            None,
            environ={
                "SEC_PROVIDER": "openai",
                "SEC_MODEL": "test-model",
                "OPENAI_API_KEY": "test-key",
                "SEC_SESSION_STORE_BACKEND": "database",
                "SEC_DATABASE_DRIVER": "opengauss+asyncpg",
                "SEC_DATABASE_HOST": "db.example.invalid",
                "SEC_DATABASE_PORT": "5432",
                "SEC_DATABASE_USERNAME": "sec-user",
                "SEC_DATABASE_PASSWORD": "secret",
                "SEC_DATABASE_NAME": "sec",
                "SEC_DATABASE_SCHEMA": "sec_history",
            },
        )

        with patch("sec.apis.client.Database") as database_factory:
            create_database(config)

        database_factory.assert_called_once_with(
            config.storage.sqlalchemy_url,
            connect_args={"server_settings": {"search_path": "sec_history"}},
        )

    def test_database_schema_rejects_other_drivers_and_unsafe_names(self) -> None:
        sqlite_config = Config.from_env(
            None,
            environ={
                "SEC_PROVIDER": "openai",
                "SEC_MODEL": "test-model",
                "OPENAI_API_KEY": "test-key",
                "SEC_SESSION_STORE_BACKEND": "database",
                "SEC_DATABASE_URL": "sqlite+aiosqlite:///sec.db",
                "SEC_DATABASE_SCHEMA": "sec",
            },
        )
        with self.assertRaisesRegex(ConfigError, "requires the opengauss"):
            create_database(sqlite_config)

        with self.assertRaisesRegex(ConfigError, "simple SQL identifier"):
            StorageSettings(
                session_store_backend="database",
                database_driver="opengauss+asyncpg",
                database_host="db.example.invalid",
                database_port=5432,
                database_username="sec-user",
                database_password="secret",
                database_name="sec",
                database_schema="sec, public",
            )

    def test_config_can_be_fully_declared_without_environment_loading(self) -> None:
        config = Config(
            provider=ProviderSettings(
                name="anthropic",
                model="explicit-model",
                api_key="explicit-secret",
            ),
            retry=RetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=1,
            ),
            storage=StorageSettings(session_store_backend="in_memory"),
        )

        self.assertEqual(config.provider.model, "explicit-model")
        self.assertEqual(config.retry.max_attempts, 2)
        self.assertEqual(config.storage, StorageSettings())

    def test_storage_settings_require_database_url_for_database_backend(self) -> None:
        with self.assertRaisesRegex(ConfigError, "SEC_DATABASE_URL"):
            StorageSettings(session_store_backend="database")
        with self.assertRaisesRegex(ConfigError, "SEC_DATABASE_URL requires"):
            StorageSettings(
                session_store_backend="in_memory",
                database_url="sqlite+aiosqlite:///ignored.db",
            )

    def test_anthropic_selection_and_shared_overrides(self) -> None:
        config = Config.from_env(
            None,
            environ={
                "SEC_PROVIDER": "anthropic",
                "SEC_MODEL": "shared-model",
                "SEC_API_KEY": "shared-secret",
                "SEC_BASE_URL": "https://example.invalid",
                "SEC_MAX_OUTPUT_TOKENS": "2048",
                "SEC_REQUEST_TIMEOUT_SECONDS": "15.5",
            },
        )

        self.assertEqual(config.provider.name, "anthropic")
        self.assertEqual(config.provider.model, "shared-model")
        self.assertEqual(config.provider.api_key, "shared-secret")
        self.assertEqual(config.provider.base_url, "https://example.invalid")
        self.assertEqual(config.provider.max_output_tokens, 2048)
        self.assertEqual(config.provider.request_timeout_seconds, 15.5)

    def test_invalid_or_missing_provider_configuration_is_explicit(self) -> None:
        with self.assertRaisesRegex(ConfigError, "SEC_PROVIDER"):
            Config.from_env(None, environ={"SEC_PROVIDER": "unknown"})

        with self.assertRaisesRegex(ConfigError, "OPENAI_MODEL"):
            Config.from_env(
                None,
                environ={"OPENAI_API_KEY": "test-secret"},
            )

        with self.assertRaisesRegex(ConfigError, "SEC_EVENT_BUFFER_SIZE"):
            Config.from_env(
                None,
                environ={
                    "OPENAI_MODEL": "test-model",
                    "OPENAI_API_KEY": "test-secret",
                    "SEC_EVENT_BUFFER_SIZE": "0",
                },
            )

        with self.assertRaisesRegex(ConfigError, "SEC_MODEL_MAX_ATTEMPTS"):
            Config.from_env(
                None,
                environ={
                    "OPENAI_MODEL": "test-model",
                    "OPENAI_API_KEY": "test-secret",
                    "SEC_MODEL_MAX_ATTEMPTS": "0",
                },
            )

        with self.assertRaisesRegex(ConfigError, "retry base delay"):
            RetrySettings(base_delay_seconds=2, max_delay_seconds=1)
        with self.assertRaisesRegex(ConfigError, "tool retry base delay"):
            ToolRetrySettings(base_delay_seconds=2, max_delay_seconds=1)

    def test_api_factory_applies_agent_defaults(self) -> None:
        config = Config(
            provider=ProviderSettings(
                name="openai",
                model="test-model",
                api_key="test-secret",
            ),
        )
        agent = create_agent(config, "configured", "Follow instructions.")
        self.assertEqual(agent.max_model_calls, 8)
        self.assertEqual(agent.max_tool_calls, 32)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    """验证两个供应商的规范消息转换和流式响应组装。"""

    async def test_configured_harness_uses_runtime_settings_and_closes_provider(self) -> None:
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace()),
            close=AsyncMock(),
        )
        config = Config(
            provider=ProviderSettings(
                name="openai",
                model="test-model",
                api_key="test-secret",
            ),
            runtime=RuntimeSettings(event_buffer_size=7),
            retry=RetrySettings(
                max_attempts=2,
                base_delay_seconds=0,
                max_delay_seconds=0,
            ),
        )
        with patch("sec.providers.openai.AsyncOpenAI", return_value=client):
            harness = create_harness(config)

        self.assertEqual(harness._event_buffer_size, 7)
        self.assertEqual(harness._retry_settings.max_attempts, 2)
        await harness.aclose()
        client.close.assert_awaited_once()

    async def test_retryable_status_error_preserves_retry_after(self) -> None:
        error = retryable_status_error(
            "OpenAI",
            429,
            {"retry-after": "1.25"},
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.retry_after_seconds, 1.25)
        self.assertIsNone(retryable_status_error("OpenAI", 400, {}))

    async def test_openai_connection_error_is_normalized_for_runtime_retry(self) -> None:
        error = OpenAIAPIConnectionError(
            message="connection lost",
            request=SimpleNamespace(),
        )
        completions = SimpleNamespace(create=AsyncMock(side_effect=error))
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
            close=AsyncMock(),
        )
        with patch("sec.providers.openai.AsyncOpenAI", return_value=client):
            provider = OpenAIProvider(
                ProviderSettings(
                    name="openai",
                    model="test-model",
                    api_key="test-secret",
                )
            )

        with self.assertRaisesRegex(RetryableProviderError, "connection error"):
            [
                item
                async for item in provider.stream(
                    [_user_message("hello")],
                    [],
                )
            ]

    async def test_anthropic_connection_error_is_normalized_for_runtime_retry(self) -> None:
        error = AnthropicAPIConnectionError(
            message="connection lost",
            request=SimpleNamespace(),
        )

        class Messages:
            """模拟在流式调用中抛出指定错误的客户端消息接口。"""

            def stream(self, **kwargs: Any) -> Any:
                raise error

        client = SimpleNamespace(
            messages=Messages(),
            close=AsyncMock(),
        )
        with patch("sec.providers.anthropic.AsyncAnthropic", return_value=client):
            provider = AnthropicProvider(
                ProviderSettings(
                    name="anthropic",
                    model="test-model",
                    api_key="test-secret",
                )
            )

        with self.assertRaisesRegex(RetryableProviderError, "connection error"):
            [
                item
                async for item in provider.stream(
                    [_user_message("hello")],
                    [],
                )
            ]
    async def test_openai_stream_assembles_tool_call_and_usage(self) -> None:
        usage = SimpleNamespace(
            model_dump=lambda **kwargs: {"prompt_tokens": 4, "completion_tokens": 2}
        )
        stream = FakeOpenAIStream(
            [
                openai_chunk(text_delta("Hello ")),
                openai_chunk(
                    SimpleNamespace(
                        content=None,
                        tool_calls=[
                            tool_delta(
                                index=0,
                                call_id="call-1",
                                name="lookup",
                                arguments='{"city":',
                            )
                        ],
                    )
                ),
                openai_chunk(
                    SimpleNamespace(
                        content=None,
                        tool_calls=[
                            tool_delta(
                                index=0,
                                arguments='"Paris"}',
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                ),
                openai_chunk(usage=usage, has_choice=False),
            ]
        )
        completions = SimpleNamespace(
            create=AsyncMock(return_value=stream),
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
            close=AsyncMock(),
        )
        settings = ProviderSettings(
            name="openai",
            model="test-model",
            api_key="test-secret",
        )
        with patch("sec.providers.openai.AsyncOpenAI", return_value=client):
            provider = OpenAIProvider(settings)

        definitions = (
            ToolDefinition(
                name="lookup",
                description="Look up a city.",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
                output_schema={"type": "string"},
            ),
        )
        events = [
            event
            async for event in provider.stream(
                [_user_message("Hello")],
                definitions,
            )
        ]

        self.assertIsInstance(events[0], ModelTextDelta)
        self.assertEqual(events[0].text, "Hello ")
        self.assertIsInstance(events[-1], ModelResponseCompleted)
        response = events[-1].message
        self.assertEqual(response.text, "Hello ")
        self.assertEqual(response.tool_uses[0].id, "call-1")
        self.assertEqual(response.tool_uses[0].arguments, {"city": "Paris"})
        self.assertEqual(response.usage.input, 4)
        self.assertEqual(response.usage.output, 2)
        self.assertTrue(response.metadata["usage_reported"])
        self.assertEqual(completions.create.await_args.kwargs["stream"], True)
        self.assertEqual(completions.create.await_args.kwargs["tools"][0]["function"]["name"], "lookup")
        self.assertTrue(stream.closed)
        await provider.aclose()
        client.close.assert_awaited_once()

    async def test_openai_rejects_stream_without_finish_marker(self) -> None:
        stream = FakeOpenAIStream([openai_chunk(text_delta("Partial"))])
        completions = SimpleNamespace(create=AsyncMock(return_value=stream))
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
            close=AsyncMock(),
        )
        settings = ProviderSettings(
            name="openai",
            model="test-model",
            api_key="test-secret",
        )
        with patch("sec.providers.openai.AsyncOpenAI", return_value=client):
            provider = OpenAIProvider(settings)

        with self.assertRaisesRegex(ProviderProtocolError, "completed choice"):
            [
                event
                async for event in provider.stream(
                    [_user_message("Hello")],
                    (),
                )
            ]
        self.assertTrue(stream.closed)

    async def test_anthropic_stream_maps_tools_and_merges_adjacent_messages(self) -> None:
        final_message = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Checking"),
                SimpleNamespace(
                    type="tool_use",
                    id="tool-1",
                    name="lookup",
                    input={"city": "Paris"},
                ),
            ],
            model="test-model",
            id="message-1",
            stop_reason="tool_use",
            usage=SimpleNamespace(
                model_dump=lambda **kwargs: {"input_tokens": 5, "output_tokens": 3}
            ),
        )
        stream = FakeAnthropicStream(["Checking"], final_message)
        request_args: dict[str, Any] = {}

        def open_stream(**kwargs: Any) -> FakeAnthropicStream:
            request_args.update(kwargs)
            return stream

        messages_api = SimpleNamespace(stream=open_stream)
        client = SimpleNamespace(
            messages=messages_api,
            close=AsyncMock(),
        )
        settings = ProviderSettings(
            name="anthropic",
            model="test-model",
            api_key="test-secret",
        )
        with patch("sec.providers.anthropic.AsyncAnthropic", return_value=client):
            provider = AnthropicProvider(settings)

        tool = ToolDefinition(
            name="lookup",
            description="Look up a city.",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "string"},
        )
        events = [
            event
            async for event in provider.stream(
                [
                    SystemMessage([TextContent("System rule")], 1.0),
                    _user_message("First"),
                    _user_message("Second"),
                ],
                (tool,),
            )
        ]
        request = request_args

        self.assertEqual(events[0], ModelTextDelta("Checking"))
        self.assertIsInstance(events[-1], ModelResponseCompleted)
        self.assertEqual(events[-1].message.tool_uses[0].arguments, {"city": "Paris"})
        self.assertEqual(request["system"], "System rule")
        self.assertEqual(len(request["messages"]), 1)
        self.assertEqual(
            [block["text"] for block in request["messages"][0]["content"]],
            ["First", "Second"],
        )
        self.assertEqual(request["tools"][0]["name"], "lookup")
        self.assertEqual(events[-1].message.usage.output, 3)
        self.assertTrue(stream.closed)
        await provider.aclose()
        client.close.assert_awaited_once()
