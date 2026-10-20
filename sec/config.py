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
    """保存运行时事件缓冲与工作区根目录配置。"""

    event_buffer_size: int = 128
    root_path: Path | None = None

    def __post_init__(self) -> None:
        if self.event_buffer_size < 1:
            raise ConfigError("event buffer size must be at least 1")
        if self.root_path is not None:
            if not isinstance(self.root_path, Path):
                raise ConfigError("SEC_ROOT_PATH must be a filesystem path")
            object.__setattr__(
                self,
                "root_path",
                self.root_path.expanduser().resolve(),
            )


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
class MemorySettings:
    """保存长期记忆的开关、后端与检索预算配置。

    backend 取值 auto/in_memory/database；auto 跟随 SessionStore 后端并复用
    同一 Database。gate 与 consolidation 依赖模型 provider，额外消耗模型调用，
    因此整体默认关闭，需通过 SEC_MEMORY_ENABLED=true 显式开启。检索预算默认
    2048；USER.md 路径由 Client 根据 SEC_ROOT_PATH/workspace 提供默认值。
    """

    enabled: bool = False
    backend: str = "auto"
    top_k: int = 5
    token_budget: int = 2048
    user_md_path: str | None = None
    gate_enabled: bool = True
    consolidation_enabled: bool = True
    procedural_consolidation_enabled: bool = True
    vector_dim: int = 1536

    def __post_init__(self) -> None:
        if self.backend not in {"auto", "in_memory", "database"}:
            raise ConfigError("memory backend must be auto, in_memory, or database")
        if self.top_k < 1:
            raise ConfigError("memory top_k must be at least 1")
        if self.token_budget is not None and self.token_budget < 1:
            raise ConfigError("memory token_budget must be at least 1")
        if self.vector_dim < 1:
            raise ConfigError("memory vector_dim must be at least 1")
        if self.user_md_path is not None and (
            not isinstance(self.user_md_path, str) or not self.user_md_path.strip()
        ):
            raise ConfigError("SEC_MEMORY_USER_MD_PATH must not be empty")


@dataclass(frozen=True)
class EmbeddingSettings:
    """保存自托管 OpenAI 兼容 embedding 服务的连接配置。

    仅当记忆后端为向量方言（opengauss）时需要；SQLite 后端只做 FTS5，
    无需 embedding 服务。
    """

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = field(default=None, repr=False)
    request_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        for key, value in (
            ("SEC_EMBEDDING_BASE_URL", self.base_url),
            ("SEC_EMBEDDING_MODEL", self.model),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ConfigError(f"{key} must not be empty")
        if self.api_key is not None and not isinstance(self.api_key, str):
            raise ConfigError("SEC_EMBEDDING_API_KEY must be text")
        if (
            not math.isfinite(self.request_timeout_seconds)
            or self.request_timeout_seconds <= 0
        ):
            raise ConfigError(
                "embedding request timeout must be a positive finite number"
            )
        if (self.base_url is None) != (self.model is None):
            raise ConfigError(
                "SEC_EMBEDDING_BASE_URL and SEC_EMBEDDING_MODEL must be set together"
            )


@dataclass(frozen=True)
class AuxiliarySettings:
    """保存辅助推理（小模型）步骤的开关与单轮调用预算。

    enabled 为总开关；intent/rewrite/judge/trace_summary 分别控制意图分类与
    内容路由、检索查询改写、LLM-as-Judge 输出判断、trace 摘要。辅助步骤不
    计入 Agent 的 max_model_calls 预算，改用 max_calls_per_turn 独立限制。
    """

    enabled: bool = False
    intent_enabled: bool = True
    rewrite_enabled: bool = True
    judge_enabled: bool = False
    trace_summary_enabled: bool = False
    max_calls_per_turn: int = 4

    def __post_init__(self) -> None:
        if self.max_calls_per_turn < 1:
            raise ConfigError("aux max calls per turn must be at least 1")


@dataclass(frozen=True)
class Config:
    """封装 sec 的分层配置值。

    可由 from_env() 加载环境配置，也可通过 Config(...) 完全显式构造；
    Agent 预算、事件容量、重试策略、上下文、存储和记忆配置均有默认值。
    """

    provider: ProviderSettings
    small_provider: ProviderSettings | None = None
    agent: AgentSettings = field(default_factory=AgentSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    retry: RetrySettings = field(default_factory=RetrySettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    tool_retry: ToolRetrySettings = field(default_factory=ToolRetrySettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    auxiliary: AuxiliarySettings = field(default_factory=AuxiliarySettings)

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
        main_provider = _load_provider_settings(
            file_values,
            environment,
            values,
            provider_names,
        )
        return cls(
            provider=main_provider,
            small_provider=_load_small_provider_settings(values, main_provider),
            agent=_load_agent_settings(values),
            runtime=_load_runtime_settings(values),
            retry=_load_retry_settings(values),
            context=_load_context_settings(values),
            tool_retry=_load_tool_retry_settings(values),
            storage=_load_storage_settings(values, storage_backends),
            memory=_load_memory_settings(values),
            embedding=_load_embedding_settings(values),
            auxiliary=_load_auxiliary_settings(values),
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
    root_path = _optional_raw(values, "SEC_ROOT_PATH")
    return RuntimeSettings(
        event_buffer_size=_positive_int(
            values,
            "SEC_EVENT_BUFFER_SIZE",
            default=128,
        ),
        root_path=Path(root_path).expanduser() if root_path is not None else None,
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


def _load_memory_settings(values: Mapping[str, str]) -> MemorySettings:
    return MemorySettings(
        enabled=_boolean(values, "SEC_MEMORY_ENABLED", default=False),
        backend=_nonempty(values, "SEC_MEMORY_BACKEND", default="auto").lower(),
        top_k=_positive_int(values, "SEC_MEMORY_TOP_K", default=5),
        token_budget=_positive_int(
            values,
            "SEC_MEMORY_TOKEN_BUDGET",
            default=2048,
        ),
        user_md_path=_optional_raw(values, "SEC_MEMORY_USER_MD_PATH"),
        gate_enabled=_boolean(values, "SEC_MEMORY_GATE_ENABLED", default=True),
        consolidation_enabled=_boolean(
            values,
            "SEC_MEMORY_CONSOLIDATION_ENABLED",
            default=True,
        ),
        procedural_consolidation_enabled=_boolean(
            values,
            "SEC_MEMORY_PROCEDURAL_CONSOLIDATION_ENABLED",
            default=True,
        ),
        vector_dim=_positive_int(values, "SEC_MEMORY_VECTOR_DIM", default=1536),
    )


def _load_embedding_settings(values: Mapping[str, str]) -> EmbeddingSettings:
    return EmbeddingSettings(
        base_url=_first_nonempty(values, "SEC_EMBEDDING_BASE_URL"),
        model=_first_nonempty(values, "SEC_EMBEDDING_MODEL"),
        api_key=_optional_raw(values, "SEC_EMBEDDING_API_KEY"),
        request_timeout_seconds=_positive_float(
            values,
            "SEC_EMBEDDING_REQUEST_TIMEOUT_SECONDS",
            default=60.0,
        ),
    )


def _load_small_provider_settings(
    values: Mapping[str, str],
    main: ProviderSettings,
) -> ProviderSettings | None:
    """读取可选的小模型层；未设置 SEC_SMALL_MODEL 时关闭（返回 None）。"""
    small_model = _first_nonempty(values, "SEC_SMALL_MODEL")
    if small_model is None:
        return None
    name = _nonempty(values, "SEC_SMALL_PROVIDER", default=main.name).lower()
    if re.fullmatch(r"[a-z][a-z0-9-]*", name) is None:
        raise ConfigError("SEC_SMALL_PROVIDER must be a lowercase kebab-case name")
    api_key = _first_nonempty(values, "SEC_SMALL_API_KEY")
    if api_key is None:
        api_key = main.api_key
    base_url = _first_nonempty(values, "SEC_SMALL_BASE_URL")
    if base_url is None:
        base_url = main.base_url
    return ProviderSettings(
        name=name,
        model=small_model,
        api_key=api_key,
        base_url=base_url,
        request_timeout_seconds=main.request_timeout_seconds,
        max_output_tokens=main.max_output_tokens,
    )


def _load_auxiliary_settings(values: Mapping[str, str]) -> AuxiliarySettings:
    return AuxiliarySettings(
        enabled=_boolean(values, "SEC_AUX_ENABLED", default=False),
        intent_enabled=_boolean(values, "SEC_AUX_INTENT_ENABLED", default=True),
        rewrite_enabled=_boolean(values, "SEC_AUX_REWRITE_ENABLED", default=True),
        judge_enabled=_boolean(values, "SEC_AUX_JUDGE_ENABLED", default=False),
        trace_summary_enabled=_boolean(
            values,
            "SEC_AUX_TRACE_SUMMARY_ENABLED",
            default=False,
        ),
        max_calls_per_turn=_positive_int(
            values,
            "SEC_AUX_MAX_CALLS_PER_TURN",
            default=4,
        ),
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


def _boolean(
    values: Mapping[str, str],
    key: str,
    *,
    default: bool,
) -> bool:
    value = _first_nonempty(values, key)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean (true/false)")


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
