"""读取、校验并组织 sec 的分层配置。

环境加载优先级为进程环境变量、项目 .env、代码默认值；应用门面支持的显式参数
优先于这些环境来源。凭据和数据库 URL 不会进入配置对象的 repr 或错误文本。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL

ProviderName = str
SessionStoreBackend = str


class ConfigError(ValueError):
    """sec 配置缺失或无效时抛出。"""


@dataclass(frozen=True)
class ProviderSettings:
    """保存所选模型 Provider 的凭据、模型和请求限制。"""

    name: ProviderName
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    request_timeout_seconds: float = 60.0
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9-]*", self.name) is None:
            raise ConfigError("provider name must be lowercase kebab-case")
        if not self.model.strip():
            raise ConfigError("provider model must not be empty")
        if not self.api_key.strip():
            raise ConfigError("provider API key must not be empty")
        if (
            not math.isfinite(self.request_timeout_seconds)
            or self.request_timeout_seconds <= 0
        ):
            raise ConfigError("request timeout must be a positive finite number")
        if self.max_output_tokens < 1:
            raise ConfigError("max output tokens must be at least 1")


@dataclass(frozen=True)
class AgentSettings:
    """保存新建 Agent 使用的模型调用和工具调用默认上限。"""

    max_model_calls: int = 8
    max_tool_calls: int = 32

    def __post_init__(self) -> None:
        if self.max_model_calls < 1:
            raise ConfigError("max model calls must be at least 1")
        if self.max_tool_calls < 1:
            raise ConfigError("max tool calls must be at least 1")


@dataclass(frozen=True)
class RetrySettings:
    """保存模型请求的有限重试次数与退避间隔。"""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("model max attempts must be at least 1")
        for name, value in (
            ("retry base delay", self.base_delay_seconds),
            ("retry max delay", self.max_delay_seconds),
        ):
            if not math.isfinite(value) or value < 0:
                raise ConfigError(f"{name} must be a non-negative finite number")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ConfigError(
                "retry base delay must not exceed retry max delay"
            )


@dataclass(frozen=True)
class ToolRetrySettings:
    """保存显式安全工具的有限重试次数与退避间隔。"""

    max_attempts: int = 1
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("tool max attempts must be at least 1")
        for name, value in (
            ("tool retry base delay", self.base_delay_seconds),
            ("tool retry max delay", self.max_delay_seconds),
        ):
            if not math.isfinite(value) or value < 0:
                raise ConfigError(f"{name} must be a non-negative finite number")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ConfigError(
                "tool retry base delay must not exceed tool retry max delay"
            )


@dataclass(frozen=True)
class RuntimeSettings:
    """保存 Harness 的运行时事件缓冲配置。"""

    event_buffer_size: int = 128

    def __post_init__(self) -> None:
        if self.event_buffer_size < 1:
            raise ConfigError("event buffer size must be at least 1")


@dataclass(frozen=True)
class ContextSettings:
    """保存模型上下文窗口及压缩触发比例。"""

    context_window_tokens: int | None = None
    compression_threshold: float = 0.8

    def __post_init__(self) -> None:
        if self.context_window_tokens is not None and self.context_window_tokens < 1:
            raise ConfigError("context window tokens must be at least 1")
        if (
            not math.isfinite(self.compression_threshold)
            or not 0 < self.compression_threshold <= 1
        ):
            raise ConfigError("context compression threshold must be in (0, 1]")


@dataclass(frozen=True)
class StorageSettings:
    """保存 SessionStore 后端选择及数据库连接配置。"""

    session_store_backend: SessionStoreBackend = "in_memory"
    database_url: str | None = field(default=None, repr=False)
    database_driver: str | None = None
    database_host: str | None = None
    database_port: int | None = None
    database_username: str | None = None
    database_password: str | None = field(default=None, repr=False)
    database_name: str | None = None
    database_schema: str | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]*", self.session_store_backend) is None:
            raise ConfigError("session store backend name is invalid")
        if self.database_url is not None and (
            not isinstance(self.database_url, str) or not self.database_url.strip()
        ):
            raise ConfigError("database URL must not be empty")
        if self.database_url is not None:
            object.__setattr__(self, "database_url", self.database_url.strip())

        for key, value in (
            ("SEC_DATABASE_DRIVER", self.database_driver),
            ("SEC_DATABASE_HOST", self.database_host),
            ("SEC_DATABASE_USERNAME", self.database_username),
            ("SEC_DATABASE_NAME", self.database_name),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ConfigError(f"{key} must not be empty")
        if self.database_password is not None and not isinstance(
            self.database_password, str
        ):
            raise ConfigError("SEC_DATABASE_PASSWORD must be text")
        if self.database_schema is not None:
            if (
                not isinstance(self.database_schema, str)
                or not self.database_schema.strip()
                or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_$]*",
                    self.database_schema.strip(),
                )
                is None
            ):
                raise ConfigError(
                    "SEC_DATABASE_SCHEMA must be a simple SQL identifier"
                )
            object.__setattr__(
                self,
                "database_schema",
                self.database_schema.strip(),
            )
        if self.database_port is not None and (
            type(self.database_port) is not int
            or not 1 <= self.database_port <= 65535
        ):
            raise ConfigError("SEC_DATABASE_PORT must be an integer from 1 to 65535")

        connection_fields = (
            self.database_driver,
            self.database_host,
            self.database_port,
            self.database_username,
            self.database_password,
            self.database_name,
        )
        has_connection_fields = any(value is not None for value in connection_fields)
        if self.database_url is not None and has_connection_fields:
            raise ConfigError(
                "use either SEC_DATABASE_URL or separate SEC_DATABASE_* connection fields"
            )

        missing_fields = [
            key
            for key, value in (
                ("SEC_DATABASE_DRIVER", self.database_driver),
                ("SEC_DATABASE_HOST", self.database_host),
                ("SEC_DATABASE_PORT", self.database_port),
                ("SEC_DATABASE_USERNAME", self.database_username),
                ("SEC_DATABASE_PASSWORD", self.database_password),
                ("SEC_DATABASE_NAME", self.database_name),
            )
            if value is None
        ]
        if has_connection_fields and missing_fields:
            raise ConfigError(
                "incomplete database connection settings; set "
                + ", ".join(missing_fields)
            )

        if (
            self.session_store_backend == "database"
            and self.database_url is None
            and not has_connection_fields
        ):
            raise ConfigError(
                "set SEC_DATABASE_URL or all SEC_DATABASE_* connection fields "
                "for the database SessionStore backend"
            )
        if self.session_store_backend == "in_memory" and self.database_url is not None:
            raise ConfigError(
                "SEC_DATABASE_URL requires the database SessionStore backend"
            )
        if self.session_store_backend == "in_memory" and has_connection_fields:
            raise ConfigError(
                "SEC_DATABASE_* connection settings require the database SessionStore backend"
            )
        if (
            self.session_store_backend == "in_memory"
            and self.database_schema is not None
        ):
            raise ConfigError(
                "SEC_DATABASE_SCHEMA requires the database SessionStore backend"
            )

    @property
    def sqlalchemy_url(self) -> str | URL | None:
        """Return the configured URL, safely quoting split credential fields."""
        if self.database_url is not None:
            return self.database_url
        if (
            self.database_driver is None
            or self.database_host is None
            or self.database_port is None
            or self.database_username is None
            or self.database_password is None
            or self.database_name is None
        ):
            return None
        return URL.create(
            drivername=self.database_driver,
            username=self.database_username,
            password=self.database_password,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )


@dataclass(frozen=True)
class Config:
    """封装 sec 的分层配置值。

    可由 from_env() 加载环境配置，也可通过 Config(...) 完全显式构造；
    Agent 预算、事件容量、重试策略、上下文和存储配置均有默认值。
    """

    provider: ProviderSettings
    agent: AgentSettings = field(default_factory=AgentSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    retry: RetrySettings = field(default_factory=RetrySettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    tool_retry: ToolRetrySettings = field(default_factory=ToolRetrySettings)
    storage: StorageSettings = field(default_factory=StorageSettings)

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = ".env",
        *,
        environ: Mapping[str, str] | None = None,
        provider_names: Collection[str] = (),
        storage_backends: Collection[str] = (),
    ) -> Config:
        """按进程环境变量、dotenv、代码默认值的顺序加载配置。"""
        file_values: dict[str, str] = {}
        if env_file is not None:
            file_values.update(
                {
                    key: value
                    for key, value in dotenv_values(env_file).items()
                    if value is not None
                }
            )
        environment = os.environ if environ is None else environ
        values = {**file_values, **environment}
        return cls(
            provider=_load_provider_settings(
                file_values,
                environment,
                values,
                provider_names,
            ),
            agent=_load_agent_settings(values),
            runtime=_load_runtime_settings(values),
            retry=_load_retry_settings(values),
            context=_load_context_settings(values),
            tool_retry=_load_tool_retry_settings(values),
            storage=_load_storage_settings(values, storage_backends),
        )


def _load_provider_settings(
    file_values: Mapping[str, str],
    environment: Mapping[str, str],
    values: Mapping[str, str],
    extension_providers: Collection[str] = (),
) -> ProviderSettings:
    provider_name = _nonempty(values, "SEC_PROVIDER", default="openai").lower()
    if re.fullmatch(r"[a-z][a-z0-9-]*", provider_name) is None or provider_name not in {
        "openai",
        "anthropic",
        *extension_providers,
    }:
        raise ConfigError("SEC_PROVIDER must name a built-in or enabled extension provider")
    name: ProviderName = provider_name
    provider_prefix = provider_name.upper().replace("-", "_")
    model = _first_nonempty(environment, "SEC_MODEL", f"{provider_prefix}_MODEL")
    if model is None:
        model = _first_nonempty(
            file_values,
            "SEC_MODEL",
            f"{provider_prefix}_MODEL",
        )
    if model is None:
        raise ConfigError(
            f"set SEC_MODEL or {provider_prefix}_MODEL for the selected provider"
        )

    api_key = _first_nonempty(
        environment,
        "SEC_API_KEY",
        f"{provider_prefix}_API_KEY",
    )
    if api_key is None:
        api_key = _first_nonempty(
            file_values,
            "SEC_API_KEY",
            f"{provider_prefix}_API_KEY",
        )
    if api_key is None:
        raise ConfigError(
            f"set SEC_API_KEY or {provider_prefix}_API_KEY for the selected provider"
        )

    base_url = _first_nonempty(
        environment,
        "SEC_BASE_URL",
        f"{provider_prefix}_BASE_URL",
    )
    if base_url is None:
        base_url = _first_nonempty(
            file_values,
            "SEC_BASE_URL",
            f"{provider_prefix}_BASE_URL",
        )

    return ProviderSettings(
        name=name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        request_timeout_seconds=_positive_float(
            values,
            "SEC_REQUEST_TIMEOUT_SECONDS",
            default=60.0,
        ),
        max_output_tokens=_positive_int(
            values,
            "SEC_MAX_OUTPUT_TOKENS",
            default=4096,
        ),
    )


def _load_agent_settings(values: Mapping[str, str]) -> AgentSettings:
    return AgentSettings(
        max_model_calls=_positive_int(
            values,
            "SEC_AGENT_MAX_MODEL_CALLS",
            default=8,
        ),
        max_tool_calls=_positive_int(
            values,
            "SEC_AGENT_MAX_TOOL_CALLS",
            default=32,
        ),
    )


def _load_runtime_settings(values: Mapping[str, str]) -> RuntimeSettings:
    return RuntimeSettings(
        event_buffer_size=_positive_int(
            values,
            "SEC_EVENT_BUFFER_SIZE",
            default=128,
        )
    )


def _load_retry_settings(values: Mapping[str, str]) -> RetrySettings:
    return RetrySettings(
        max_attempts=_positive_int(
            values,
            "SEC_MODEL_MAX_ATTEMPTS",
            default=3,
        ),
        base_delay_seconds=_nonnegative_float(
            values,
            "SEC_MODEL_RETRY_BASE_DELAY_SECONDS",
            default=0.5,
        ),
        max_delay_seconds=_nonnegative_float(
            values,
            "SEC_MODEL_RETRY_MAX_DELAY_SECONDS",
            default=8.0,
        ),
    )


def _load_context_settings(values: Mapping[str, str]) -> ContextSettings:
    return ContextSettings(
        context_window_tokens=_optional_positive_int(
            values,
            "SEC_CONTEXT_WINDOW_TOKENS",
        ),
        compression_threshold=_ratio(
            values,
            "SEC_CONTEXT_COMPRESSION_THRESHOLD",
            default=0.8,
        ),
    )


def _load_tool_retry_settings(values: Mapping[str, str]) -> ToolRetrySettings:
    return ToolRetrySettings(
        max_attempts=_positive_int(
            values,
            "SEC_TOOL_MAX_ATTEMPTS",
            default=1,
        ),
        base_delay_seconds=_nonnegative_float(
            values,
            "SEC_TOOL_RETRY_BASE_DELAY_SECONDS",
            default=0.5,
        ),
        max_delay_seconds=_nonnegative_float(
            values,
            "SEC_TOOL_RETRY_MAX_DELAY_SECONDS",
            default=8.0,
        ),
    )


def _load_storage_settings(
    values: Mapping[str, str],
    extension_backends: Collection[str] = (),
) -> StorageSettings:
    raw_backend = _nonempty(
        values,
        "SEC_SESSION_STORE_BACKEND",
        default="in_memory",
    ).lower()
    if re.fullmatch(r"[a-z][a-z0-9_-]*", raw_backend) is None or raw_backend not in {
        "in_memory",
        "database",
        *extension_backends,
    }:
        raise ConfigError(
            "SEC_SESSION_STORE_BACKEND must name a built-in or enabled extension backend"
        )
    backend: SessionStoreBackend = raw_backend
    return StorageSettings(
        session_store_backend=backend,
        database_url=_first_nonempty(values, "SEC_DATABASE_URL"),
        database_driver=_first_nonempty(values, "SEC_DATABASE_DRIVER"),
        database_host=_first_nonempty(values, "SEC_DATABASE_HOST"),
        database_port=_optional_positive_int(values, "SEC_DATABASE_PORT"),
        database_username=_first_nonempty(values, "SEC_DATABASE_USERNAME"),
        database_password=_optional_raw(values, "SEC_DATABASE_PASSWORD"),
        database_name=_first_nonempty(values, "SEC_DATABASE_NAME"),
        database_schema=_first_nonempty(values, "SEC_DATABASE_SCHEMA"),
    )


# 配置读取辅助函数仅返回变量名相关错误，绝不将配置值写入错误文本。
def _first_nonempty(values: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None


def _optional_raw(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key)
    if value is None or not value.strip():
        return None
    return value


def _nonempty(
    values: Mapping[str, str],
    key: str,
    *,
    default: str,
) -> str:
    value = _first_nonempty(values, key)
    return default if value is None else value


def _positive_int(
    values: Mapping[str, str],
    key: str,
    *,
    default: int,
) -> int:
    value = _first_nonempty(values, key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ConfigError(f"{key} must be an integer") from None
    if parsed < 1:
        raise ConfigError(f"{key} must be at least 1")
    return parsed


def _optional_positive_int(
    values: Mapping[str, str],
    key: str,
) -> int | None:
    value = _first_nonempty(values, key)
    if value is None:
        return None
    return _positive_int(values, key, default=1)


def _positive_float(
    values: Mapping[str, str],
    key: str,
    *,
    default: float,
) -> float:
    value = _first_nonempty(values, key)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        raise ConfigError(f"{key} must be a number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigError(f"{key} must be a positive finite number")
    return parsed


def _ratio(
    values: Mapping[str, str],
    key: str,
    *,
    default: float,
) -> float:
    value = _first_nonempty(values, key)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        raise ConfigError(f"{key} must be a number") from None
    if not math.isfinite(parsed) or not 0 < parsed <= 1:
        raise ConfigError(f"{key} must be in (0, 1]")
    return parsed


def _nonnegative_float(
    values: Mapping[str, str],
    key: str,
    *,
    default: float,
) -> float:
    value = _first_nonempty(values, key)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        raise ConfigError(f"{key} must be a number") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise ConfigError(f"{key} must be a non-negative finite number")
    return parsed
