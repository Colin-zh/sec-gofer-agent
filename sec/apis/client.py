"""Client construction, resource assembly, and application lifecycle.

This module owns the public Client implementation. The runtime and Agent domain
objects remain usable independently by internal tests and advanced integrations.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from typing import TYPE_CHECKING, Literal, Protocol

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import NoSuchModuleError

from sec.apps.extension_ui import ExtensionUIRequest, ExtensionUIResponse
from sec.agent import Agent, Trace, Session
from sec.config import (
    Config,
    ConfigError,
    ContextSettings,
    ProviderName,
    ProviderSettings,
)
from sec._runtime.harness import Harness
from sec.extensions import ExtensionCatalog, ExtensionManager
from sec.extensions.api import ProviderFactory, StorageFactory
from sec._types.context import TokenEstimator
from sec._types.execution import ToolExecutor
from sec._types.hooks import HookContext, HookHandler, HookName
from sec._types.policy import PolicyEngine
from sec.policies.rules import AutoApprovePolicy
from sec._types.embedding import Embedder
from sec._types.providers import ModelProvider, StreamingModelProvider
from sec.providers.router import ModelRouter
from sec._types.session import MessageEntry, SessionEntry, SessionInfoEntry, SessionSnapshot, SessionSummary
from sec._types.storage import SessionStoreProtocol
from sec.storage import Database, MemoryStore, SessionStore
from sec.memory import (
    MemoryConsolidator,
    MemoryGate,
    MemoryRetriever,
    MemoryRuntime,
    ProceduralConsolidator,
    SkillSource,
    UserProfile,
)
from sec.exceptions import RecoverableToolError, SecUIHandlerError
from sec.tools import Tool, tool
from sec.tools._builtins.management import RESERVED_TOOL_NAMES
from sec.agent.card import AgentCard
from sec.agent.auxiliary import AuxiliaryRuntime
from sec.agent._util import complete_text
from sec.resources.agents import AgentCatalog
from sec.resources.markdown import CliResource, ResourceManager
from sec._runtime.capabilities import (
    CapabilityBinding, CapabilitySnapshot, ReloadReport, TraceAllowance,
)
from sec._types.session import CustomEntry
from sec._runtime.resource_settings import (
    read_worker_extensions, validate_extension_names, write_worker_extensions,
)

if TYPE_CHECKING:
    from sec.extensions.worker import WorkerExtensionManager
    from sec.integrations.mcp import MCPManager


class _AsyncResource(Protocol):
    async def aclose(self) -> None: ...


@dataclass
class _DelegationRecord:
    """Bookkeeping for one active delegation, keyed by the child Trace id."""

    trace: Trace
    binding: CapabilityBinding
    actchain: tuple[str, ...]
    parent: CapabilityBinding
    sliced: TraceAllowance | None


async def _swallow_cancelled(trace: Trace) -> None:
    """Await a cancelled delegation Trace; its cancellation unwinds as normal."""
    try:
        await trace.result()
    except asyncio.CancelledError:
        pass


def build_provider(
    settings: ProviderSettings,
    config: Config,
    *,
    providers: Mapping[str, ProviderFactory] | None = None,
) -> ModelProvider | StreamingModelProvider:
    """依据指定 ProviderSettings 构建 Provider。

    扩展 Provider 的 factory 签名仍接收整个 Config；通过 dataclasses.replace
    把 config.provider 换成目标 tier 的 settings，使其读到正确的模型/凭据。
    """
    if providers is not None and settings.name in providers:
        provider = providers[settings.name](replace(config, provider=settings))
        if not (
            callable(getattr(provider, "complete", None))
            or callable(getattr(provider, "stream", None))
        ):
            raise ConfigError(
                f"provider factory {settings.name!r} returned an invalid provider"
            )
        return provider
    if settings.name == "openai":
        from sec.providers.openai import OpenAIProvider

        return OpenAIProvider(settings)
    if settings.name == "anthropic":
        from sec.providers.anthropic import AnthropicProvider

        return AnthropicProvider(settings)
    raise ConfigError(f"no enabled extension provides model provider {settings.name!r}")


def create_provider(
    config: Config,
    *,
    providers: Mapping[str, ProviderFactory] | None = None,
) -> ModelProvider | StreamingModelProvider:
    """依据配置选择并创建流式 Provider。"""
    return build_provider(config.provider, config, providers=providers)


def create_database(config: Config) -> Database | None:
    """按存储配置创建 Database；调用方负责 initialize() 和 aclose()。"""
    if config.storage.session_store_backend != "database":
        return None
    database_url = config.storage.sqlalchemy_url
    if database_url is None:
        raise ConfigError(
            "set SEC_DATABASE_URL or all SEC_DATABASE_* connection fields "
            "for the database backend"
        )
    schema = config.storage.database_schema
    parsed_url = database_url if isinstance(database_url, URL) else make_url(database_url)
    if schema is not None and parsed_url.drivername != "opengauss+asyncpg":
        raise ConfigError(
            "SEC_DATABASE_SCHEMA currently requires the opengauss+asyncpg database driver"
        )
    try:
        if schema is None:
            return Database(database_url)
        return Database(
            database_url,
            connect_args={"server_settings": {"search_path": schema}},
        )
    except (NoSuchModuleError, ModuleNotFoundError) as error:
        if parsed_url.drivername != "opengauss+asyncpg":
            raise
        if isinstance(error, ModuleNotFoundError) and error.name not in {
            "asyncpg", "opengauss", "opengauss_sqlalchemy",
        }:
            raise
        raise ConfigError(
            "The opengauss+asyncpg database driver requires the openGauss optional "
            "dependencies in the environment running SEC. Install "
            "'sec-gofer-agent[desktop,opengauss]', or in a source checkout run "
            "'uv sync --extra desktop --extra opengauss'. Preserve both extras "
            "when syncing again."
        ) from error


def create_session_store(
    config: Config,
    *,
    database: Database | None = None,
    storage_backends: Mapping[str, StorageFactory] | None = None,
) -> SessionStoreProtocol:
    """按配置创建 SessionStore；数据库后端须注入应用持有的 Database。"""
    backend = config.storage.session_store_backend
    if backend in ("in_memory", "database"):
        if storage_backends is not None and backend in storage_backends:
            raise ConfigError(f"extension cannot override built-in storage backend {backend!r}")
    elif storage_backends is not None and backend in storage_backends:
        created = storage_backends[backend](config, database)
        if not (
            callable(getattr(created, "load", None))
            and callable(getattr(created, "append", None))
            and callable(getattr(created, "list_sessions", None))
        ):
            raise ConfigError(
                f"storage factory {backend!r} returned an invalid SessionStore"
            )
        return created
    else:
        raise ConfigError(f"unknown SessionStore backend: {backend!r}")
    if backend == "in_memory":
        if database is not None:
            raise ConfigError("database is only valid for the database backend")
        return SessionStore(backend="in_memory")
    if database is None:
        raise ConfigError(
            "create and inject a Database for the database SessionStore backend"
        )
    return SessionStore(backend="database", database=database)


def create_embedder(config: Config) -> Embedder | None:
    """按 embedding 配置创建自托管 OpenAI 兼容 Embedder；未配置返回 None。"""
    settings = config.embedding
    if settings.base_url is None or settings.model is None:
        return None
    from sec.providers.embedding import OpenAICompatibleEmbedder

    return OpenAICompatibleEmbedder(
        base_url=settings.base_url,
        model=settings.model,
        api_key=settings.api_key,
        timeout=settings.request_timeout_seconds,
    )


def _resolve_memory_backend(config: Config) -> str:
    """解析记忆后端；auto 跟随 SessionStore 后端并复用同一 Database。"""
    backend = config.memory.backend
    if backend != "auto":
        return backend
    return (
        "database"
        if config.storage.session_store_backend == "database"
        else "in_memory"
    )


def create_memory_store(
    config: Config,
    *,
    database: Database | None = None,
    embedder: Embedder | None = None,
    workspace: Path | None = None,
) -> MemoryStore:
    """按记忆配置创建 MemoryStore；数据库后端须注入应用持有的 Database。

    解析为 ``in_memory`` 且提供 ``workspace`` 时改用 ``file`` 后端，把长期
    记忆 JSON 落盘到工作区 ``.sec/``；Memory 启用但未传 workspace 时使用
    SEC_ROOT_PATH 或当前目录，避免本地配置下记忆随进程消失。
    """
    backend = _resolve_memory_backend(config)
    if backend == "database":
        if database is None:
            raise ConfigError(
                "create and inject a Database for the database memory backend"
            )
        return MemoryStore(
            backend="database",
            database=database,
            embedder=embedder,
            vector_dim=config.memory.vector_dim,
        )
    effective_workspace = (
        workspace
        or config.runtime.root_path
        or (Path.cwd() if config.memory.enabled else None)
    )
    if effective_workspace is not None:
        return MemoryStore(backend="file", workspace=effective_workspace)
    return MemoryStore(backend="in_memory")


def _configure_memory_profile(config: Config, workspace: Path) -> Config:
    """Resolve and create the default workspace-scoped USER.md without overwriting it."""
    configured_path = config.memory.user_md_path
    profile_path = (
        Path(configured_path).expanduser()
        if configured_path is not None
        else workspace / ".sec" / "USER.md"
    )
    if not profile_path.is_absolute():
        profile_path = workspace / profile_path
    profile_path = profile_path.resolve()
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.touch(exist_ok=True)
    return replace(
        config,
        memory=replace(config.memory, user_md_path=str(profile_path)),
    )


def create_memory_runtime(
    config: Config,
    provider: ModelProvider | StreamingModelProvider | None,
    memory_store: MemoryStore | None,
    *,
    token_estimator: TokenEstimator | None = None,
    skills_provider: Callable[[], tuple[CliResource, ...]] | None = None,
    small_provider: ModelProvider | StreamingModelProvider | None = None,
) -> MemoryRuntime | None:
    """将 gate/retriever/consolidator 打包为可注入 Harness 的 MemoryRuntime。

    skills_provider 惰性返回当前已发现的 Skill 快照（首次装配时 capabilities
    可能尚未载入，检索时才取最新值），用于把 Skill 统一进程序性记忆来源。
    small_provider 未提供时，gate/consolidator 复用主 provider。
    """
    if memory_store is None or provider is None:
        return None
    settings = config.memory
    memory_provider = small_provider or provider
    can_complete = callable(getattr(memory_provider, "complete", None))
    gate = (
        MemoryGate(memory_provider, enabled=settings.gate_enabled)
        if can_complete
        else None
    )
    consolidator = (
        MemoryConsolidator(
            memory_provider,
            memory_store.semantic,
            enabled=settings.consolidation_enabled,
        )
        if can_complete
        else None
    )
    procedural_sources = []
    if settings.user_md_path is not None:
        procedural_sources.append(UserProfile(settings.user_md_path))
    if skills_provider is not None:
        procedural_sources.append(
            SkillSource(skills_provider, top_k=settings.top_k)
        )
    retriever = MemoryRetriever(
        memory_store.semantic,
        memory_store.episodic,
        procedural_sources=procedural_sources,
        top_k=settings.top_k,
        token_budget=settings.token_budget,
        token_estimator=token_estimator,
    )
    procedural_consolidator = (
        ProceduralConsolidator(
            memory_provider,
            settings.user_md_path,
            enabled=settings.procedural_consolidation_enabled,
        )
        if can_complete and settings.user_md_path is not None
        else None
    )
    return MemoryRuntime(
        gate=gate,
        retriever=retriever,
        episodic=memory_store.episodic,
        consolidator=consolidator,
        procedural_consolidator=procedural_consolidator,
    )


def create_auxiliary_runtime(
    config: Config,
    provider: ModelProvider | StreamingModelProvider | None,
) -> AuxiliaryRuntime | None:
    """按配置装配小模型辅助推理组件；未启用或 Provider 不支持 complete 时返回 None。

    调用方应传入小模型 provider（无小模型时传主 provider），使意图分类、
    查询改写、LLM-as-Judge、trace 摘要等辅助步骤复用同一轻量模型。
    """
    if provider is None or not config.auxiliary.enabled:
        return None
    if not callable(getattr(provider, "complete", None)):
        return None
    settings = config.auxiliary
    from sec.agent.intent import IntentClassifier
    from sec.agent.judge import OutputJudge
    from sec.agent.rewrite import QueryRewriter
    from sec.agent.router import ContentRouter
    from sec.agent.summary import TraceSummarizer

    return AuxiliaryRuntime(
        settings=settings,
        intent=IntentClassifier(provider) if settings.intent_enabled else None,
        router=ContentRouter() if settings.intent_enabled else None,
        rewriter=QueryRewriter(provider) if settings.rewrite_enabled else None,
        judge=OutputJudge(provider) if settings.judge_enabled else None,
        summarizer=(
            TraceSummarizer(provider) if settings.trace_summary_enabled else None
        ),
    )


def create_agent(
    config: Config,
    name: str,
    instructions: str,
    *,
    tools: Iterable[Tool] = (),
) -> Agent:
    """用配置中的调用预算创建 Agent。"""
    return Agent(
        name=name,
        instructions=instructions,
        tools=tuple(tools),
        max_model_calls=config.agent.max_model_calls,
        max_tool_calls=config.agent.max_tool_calls,
    )


def create_harness(
    config: Config,
    *,
    policy: PolicyEngine | None = None,
    executor: ToolExecutor | None = None,
    token_estimator: TokenEstimator | None = None,
    providers: Mapping[str, ProviderFactory] | None = None,
    hooks: Mapping[HookName, tuple[HookHandler, ...]] | None = None,
    provider: ModelProvider | StreamingModelProvider | None = None,
    memory: MemoryRuntime | None = None,
    router: ModelRouter | None = None,
    auxiliary: AuxiliaryRuntime | None = None,
) -> Harness:
    """创建应用 Harness；运行结束后需调用 aclose() 释放 Provider。

    传入 provider 可复用调用方已创建（并可能为记忆组件共享）的 Provider；
    否则按配置创建。memory 为长期记忆运行时组件，缺省时不注入。router 提供
    main/small 双 tier 模型选择，auxiliary 为可选的小模型辅助推理组件。
    """
    if (config.context.context_window_tokens is None) != (token_estimator is None):
        raise ConfigError(
            "context compression requires both context_window_tokens and "
            "a TokenEstimator"
        )
    if token_estimator is not None and not callable(
        getattr(token_estimator, "estimate_tokens", None)
    ):
        raise ConfigError("token_estimator must implement estimate_tokens()")
    if (
        config.context.context_window_tokens is not None
        and config.provider.max_output_tokens
        >= config.context.context_window_tokens
    ):
        raise ConfigError(
            "max_output_tokens must be less than context_window_tokens"
        )
    if router is not None:
        resolved_provider = router.main
    else:
        resolved_provider = (
            create_provider(config, providers=providers)
            if provider is None
            else provider
        )
    return Harness(
        resolved_provider,
        policy=policy,
        executor=executor,
        event_buffer_size=config.runtime.event_buffer_size,
        retry_settings=config.retry,
        tool_retry_settings=config.tool_retry,
        context_settings=config.context,
        token_estimator=token_estimator,
        max_output_tokens=config.provider.max_output_tokens,
        hooks=hooks,
        memory=memory,
        router=router,
        auxiliary=auxiliary,
    )


class _SecClientCore:
    """为同步和异步 Client 共享对话资源装配与生命周期逻辑。

    构造参数中的非空配置优先于进程环境变量、dotenv 和代码默认值；Client 只
    关闭自行创建的 Provider/Database，注入的 SessionStore、Database 和 Session
    始终由调用方持有。
    """

    def __init__(
        self,
        name: str = "assistant",
        instructions: str = "You are Sec, a helpful assistant.",
        *,
        provider: ProviderName | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        request_timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        max_model_calls: int | None = None,
        max_tool_calls: int | None = None,
        event_buffer_size: int | None = None,
        max_model_attempts: int | None = None,
        model_retry_base_delay_seconds: float | None = None,
        model_retry_max_delay_seconds: float | None = None,
        max_tool_attempts: int | None = None,
        tool_retry_base_delay_seconds: float | None = None,
        tool_retry_max_delay_seconds: float | None = None,
        context_window_tokens: int | None = None,
        compression_threshold: float | None = None,
        session_store_backend: str | None = None,
        database_url: str | None = None,
        env_file: str | Path | None = ".env",
        tools: Iterable[Tool] = (),
        token_estimator: TokenEstimator | None = None,
        policy: PolicyEngine | None = None,
        executor: ToolExecutor | None = None,
        policy_name: str | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        store: SessionStoreProtocol | None = None,
        database: Database | None = None,
        enabled_extensions: Iterable[str] = (),
        workspace: str | Path | None = None,
        agent_id: str | None = None,
        worker_extensions: Iterable[str] | None = None,
        memory_enabled: bool = False,
    ) -> None:
        if session is not None and any(
            value is not None for value in (session_id, store, database)
        ):
            raise ValueError(
                "session cannot be combined with session_id, store, or database"
            )
        if store is not None and database is not None:
            raise ValueError("store and database cannot both be injected")
        self._name = name
        self._instructions = instructions
        self._env_file = env_file
        self._tools = tuple(tools)
        self._token_estimator = token_estimator
        self._policy = policy
        if policy is not None and policy_name is not None:
            raise ValueError("policy and policy_name cannot both be provided")
        self._policy_name = policy_name
        self._executor = executor
        self._session = session
        self._session_id = session_id
        self._store = store
        self._database = database
        self._owns_database = False
        self._owns_store = False
        self._memory_store: MemoryStore | None = None
        self._embedder: Embedder | None = None
        self._owns_embedder = False
        self._harness: Harness | None = None
        self._agent: Agent | None = None
        self._router: ModelRouter | None = None
        self._config: Config | None = None
        self._active_trace: Trace | None = None
        self._background_traces: dict[str, Trace] = {}
        self._scheduled_policy: PolicyEngine | None = None
        self._session_switching = False
        self._enabled_extensions = tuple(enabled_extensions)
        self._extension_manager = ExtensionManager()
        self._extension_catalog: ExtensionCatalog | None = None
        self._closed = False
        self._workspace = None if workspace is None else Path(workspace).expanduser().resolve()
        if agent_id is not None and self._workspace is None:
            raise ValueError("agent_id requires a workspace")
        self._selected_agent_id = agent_id or "default"
        self._pending_agent_id: str | None = None
        self._resources: ResourceManager | None = None
        self._resource_activation_path: Path | None = None
        self._capabilities: CapabilitySnapshot | None = None
        self._mcp_manager: MCPManager | None = None
        self._worker_manager: WorkerExtensionManager | None = None
        self._worker_extensions = (
            read_worker_extensions(self._workspace)
            if worker_extensions is None and self._workspace is not None
            else validate_extension_names(tuple(worker_extensions or ()))
        )
        self._worker_config_override = (
            None if worker_extensions is None else self._worker_extensions
        )
        if self._worker_extensions and self._workspace is None:
            raise ValueError("worker_extensions requires a workspace")
        self._retired_managers: list[_AsyncResource] = []
        self._reload_lock = asyncio.Lock()
        self._resource_mutation_lock = asyncio.Lock()
        self._revoked_capabilities: set[str] = set()
        self._known_capability_names: set[str] = set()
        self._delegations: dict[str, _DelegationRecord] = {}
        explicit_values: dict[str, str] = {}
        for key, value in (
            ("SEC_PROVIDER", provider),
            ("SEC_MODEL", model),
            ("SEC_API_KEY", api_key),
            ("SEC_BASE_URL", base_url),
            ("SEC_REQUEST_TIMEOUT_SECONDS", request_timeout_seconds),
            ("SEC_MAX_OUTPUT_TOKENS", max_output_tokens),
            ("SEC_AGENT_MAX_MODEL_CALLS", max_model_calls),
            ("SEC_AGENT_MAX_TOOL_CALLS", max_tool_calls),
            ("SEC_EVENT_BUFFER_SIZE", event_buffer_size),
            ("SEC_MODEL_MAX_ATTEMPTS", max_model_attempts),
            (
                "SEC_MODEL_RETRY_BASE_DELAY_SECONDS",
                model_retry_base_delay_seconds,
            ),
            (
                "SEC_MODEL_RETRY_MAX_DELAY_SECONDS",
                model_retry_max_delay_seconds,
            ),
            ("SEC_TOOL_MAX_ATTEMPTS", max_tool_attempts),
            (
                "SEC_TOOL_RETRY_BASE_DELAY_SECONDS",
                tool_retry_base_delay_seconds,
            ),
            (
                "SEC_TOOL_RETRY_MAX_DELAY_SECONDS",
                tool_retry_max_delay_seconds,
            ),
            ("SEC_CONTEXT_WINDOW_TOKENS", context_window_tokens),
            ("SEC_CONTEXT_COMPRESSION_THRESHOLD", compression_threshold),
            ("SEC_SESSION_STORE_BACKEND", session_store_backend),
            ("SEC_DATABASE_URL", database_url),
        ):
            if value is not None:
                explicit_values[key] = str(value)
        if database_url is not None or session_store_backend == "in_memory":
            for key in (
                "SEC_DATABASE_DRIVER",
                "SEC_DATABASE_HOST",
                "SEC_DATABASE_PORT",
                "SEC_DATABASE_USERNAME",
                "SEC_DATABASE_PASSWORD",
                "SEC_DATABASE_NAME",
                "SEC_DATABASE_SCHEMA",
            ):
                explicit_values[key] = ""
        if session_store_backend == "in_memory" and database_url is None:
            explicit_values["SEC_DATABASE_URL"] = ""
        if memory_enabled:
            explicit_values["SEC_MEMORY_ENABLED"] = "1"
        self._explicit_environment = explicit_values

    async def _initialize(self) -> None:
        """加载配置、初始化所需存储并装配单段对话。"""
        if self._closed:
            raise RuntimeError("SecClient is closed")
        if self._harness is not None:
            raise RuntimeError("SecClient is already entered")
        environment = {**os.environ, **self._explicit_environment}
        try:
            await self._extension_manager.load(self._enabled_extensions)
            self._extension_catalog = self._extension_manager.catalog
            config = Config.from_env(
                self._env_file,
                environ=environment,
                provider_names=self._extension_catalog.providers.keys(),
                storage_backends=self._extension_catalog.storage_backends.keys(),
            )
            memory_workspace = (
                self._workspace or config.runtime.root_path or Path.cwd()
            ).expanduser().resolve()
            if config.memory.enabled:
                config = _configure_memory_profile(config, memory_workspace)
            if (config.context.context_window_tokens is None) != (
                self._token_estimator is None
            ):
                raise ConfigError(
                    "context compression requires both context_window_tokens and "
                    "a TokenEstimator"
                )
            if self._session is None:
                if self._store is None:
                    if self._database is None:
                        self._database = create_database(config)
                        self._owns_database = self._database is not None
                    created_store = create_session_store(
                        config,
                        database=self._database,
                        storage_backends=self._extension_catalog.storage_backends,
                    )
                    self._store = created_store
                    self._owns_store = (
                        config.storage.session_store_backend not in ("in_memory", "database")
                    )
                    initialize = getattr(created_store, "initialize", None)
                    if initialize is not None:
                        initialized = initialize()
                        if inspect.isawaitable(initialized):
                            await initialized
                self._session = (
                    await Session.restore(self._store, self._session_id)
                    if self._session_id is not None
                    else Session(store=self._store)
                )
            if config.memory.enabled:
                self._embedder = create_embedder(config)
                self._owns_embedder = self._embedder is not None
                self._memory_store = create_memory_store(
                    config,
                    database=self._database,
                    embedder=self._embedder,
                    workspace=memory_workspace,
                )
                initialize = getattr(self._memory_store, "initialize", None)
                if initialize is not None:
                    initialized = initialize()
                    if inspect.isawaitable(initialized):
                        await initialized
            self._agent, self._harness = self._build_run_resources(
                config,
                self._extension_catalog,
            )
            self._config = config
            if self._workspace is not None:
                await self._reload_resources()
                self._restore_agent_selection(self.session)
        except BaseException as initialization_error:
            try:
                await self._close()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "SecClient initialization and cleanup failed",
                    [initialization_error, cleanup_error],
                ) from None
            raise

    @property
    def session(self) -> Session:
        """返回此 Client 管理的对话 Session。"""
        if self._session is None:
            raise RuntimeError("SecClient must be entered before using session")
        return self._session

    @property
    def memory_store(self) -> MemoryStore | None:
        """返回长期记忆仓储（记忆未启用时为 None）。

        通过 ``.semantic`` / ``.episodic`` 访问语义与情景 adapter。
        """
        return self._memory_store

    @property
    def database(self) -> Database | None:
        """返回 Client 持有的共享 Database；生命周期仍由 Client 管理。"""
        return self._database

    @property
    def memory_user_profile_path(self) -> Path | None:
        """返回 Client 实际使用的 USER.md 路径；Memory 未启用或未初始化时为 None。"""
        if self._memory_store is None or self._config is None:
            return None
        path = self._config.memory.user_md_path
        return Path(path) if path is not None else None

    async def list_sessions(self, limit: int = 20) -> tuple[SessionSummary, ...]:
        """列举存储中最近更新且可恢复的会话。"""
        if self._closed or self._harness is None:
            raise RuntimeError("SecClient must be entered before listing Sessions")
        if self._store is None:
            raise RuntimeError("Session listing requires a SessionStore")
        list_sessions = getattr(self._store, "list_sessions", None)
        if not callable(list_sessions):
            raise RuntimeError("the configured SessionStore does not support listing")
        return await list_sessions(limit)

    async def switch_session(self, session_id: str | None = None) -> Session:
        """安全切换当前会话；传入 ID 时必须恢复已有的非空会话。"""
        self._require_idle_session_switch()
        store = self._require_session_store()
        self._session_switching = True
        try:
            if session_id is None:
                session = Session(store=store)
            else:
                if not isinstance(session_id, str) or not session_id.strip():
                    raise ValueError("session ID must not be empty")
                session = await Session.restore(store, session_id)
                if not session.entries:
                    raise KeyError(f"Session {session_id!r} was not found")
            if self._capabilities is not None:
                self._restore_agent_selection(session)
            self._session = session
            self._session_id = session.id
            return session
        finally:
            self._session_switching = False

    async def rename_session(self, session_id: str, name: str) -> None:
        """重命名已存储会话，不改变当前会话；活动 Trace 期间禁止修改。"""
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session ID must not be empty")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("session name must not be empty")
        self._require_idle_session_switch()
        store = self._require_session_store()
        self._session_switching = True
        try:
            session = (
                self.session if session_id == self.session.id
                else await Session.restore(store, session_id)
            )
            if session is not self.session and not session.entries:
                raise KeyError(f"Session {session_id!r} was not found")
            await session.append_entry(
                SessionInfoEntry(
                    id=uuid4().hex,
                    parent_id=session.active_leaf_id,
                    timestamp=time.time(),
                    name=name.strip(),
                )
            )
        finally:
            self._session_switching = False

    async def generate_session_title(self, text: str) -> str:
        """用小模型为会话的首条输入生成短标题；失败返回空串（fail-open）。"""
        router = self._router
        if router is None:
            return ""
        provider = router.provider("small")
        if not callable(getattr(provider, "complete", None)):
            return ""
        prompt = (text or "").strip()[:4000]
        if not prompt:
            return ""
        try:
            raw = await complete_text(provider, _TITLE_SYSTEM, prompt)
        except Exception:
            return ""
        title = raw.strip().strip("\"'“”").strip()
        return title[:40]

    async def get_session_snapshot(self, session_id: str) -> SessionSnapshot:
        """读取指定会话的快照，不改变当前会话。"""
        self._require_idle_session_switch()
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session ID must not be empty")
        snapshot = (
            self.session.snapshot if session_id == self.session.id
            else await self._require_session_store().load(session_id)
        )
        if not snapshot.entries:
            raise KeyError(f"Session {session_id!r} was not found")
        return snapshot

    async def delete_session(self, session_id: str) -> None:
        """删除指定会话；删除当前会话后创建新的空会话。"""
        self._require_idle_session_switch()
        snapshot = await self.get_session_snapshot(session_id)
        self._require_idle_session_switch()
        store = self._require_session_store()
        delete_session = getattr(store, "delete", None)
        if not callable(delete_session):
            raise RuntimeError("the configured SessionStore does not support deletion")
        self._session_switching = True
        try:
            await delete_session(session_id, snapshot.version)
            if self.session.id == session_id:
                self._session = Session(store=store)
                self._session_id = self._session.id
        finally:
            self._session_switching = False

    async def fork_session(self, session_id: str | None = None) -> Session:
        """在新会话中复制当前活动分支。"""
        return await self._copy_session(include_branches=False, session_id=session_id)

    async def clone_session(self, session_id: str | None = None) -> Session:
        """在新会话中复制完整历史树及其活动分支。"""
        return await self._copy_session(include_branches=True, session_id=session_id)

    async def import_session(self, snapshot: SessionSnapshot) -> Session:
        """将校验后的历史树作为新会话原子导入并切换过去。"""
        if not isinstance(snapshot, SessionSnapshot):
            raise TypeError("snapshot must be a SessionSnapshot")
        return await self._copy_entries_to_new_session(
            snapshot.entries,
            snapshot.active_leaf_id,
        )

    @property
    def agent(self) -> Agent:
        """返回此 Client 创建的 Agent。"""
        if self._agent is None:
            raise RuntimeError("SecClient must be entered before using agent")
        return self._agent

    @property
    def resources(self) -> ResourceManager:
        if self._resources is None:
            raise RuntimeError("resource discovery requires an initialized workspace Client")
        return self._resources

    @property
    def agent_cards(self) -> tuple[AgentCard, ...]:
        return () if self._capabilities is None else self._capabilities.cards

    @property
    def selected_agent_id(self) -> str:
        return self._selected_agent_id

    @property
    def pending_agent_id(self) -> str | None:
        return self._pending_agent_id

    @property
    def worker_extensions(self) -> tuple[str, ...]:
        return self._worker_extensions

    @property
    def available_tools(self) -> tuple[Tool, ...]:
        return self.agent.tools if self._capabilities is None else self._capabilities.tools

    @property
    def capability_generation(self) -> int:
        return 0 if self._capabilities is None else self._capabilities.generation

    @property
    def mcp_statuses(self) -> tuple:
        return () if self._mcp_manager is None else self._mcp_manager.statuses

    @property
    def worker_statuses(self) -> tuple:
        return () if self._worker_manager is None else self._worker_manager.statuses

    def _current_capabilities(self) -> CapabilitySnapshot:
        if self._capabilities is None:
            raise RuntimeError("workspace capabilities are not initialized")
        return self._capabilities

    def _restore_agent_selection(self, session: Session) -> None:
        selected = "default"
        for entry in session.snapshot.active_path:
            if (
                isinstance(entry, CustomEntry)
                and entry.custom_type == "sec.agent_selection"
                and isinstance(entry.data, dict)
                and isinstance(entry.data.get("agent_id"), str)
            ):
                selected = entry.data["agent_id"]
        if any(
            isinstance(entry, CustomEntry) and entry.custom_type == "sec.agent_selection"
            for entry in session.snapshot.active_path
        ):
            self._current_capabilities().card(selected)
            self._selected_agent_id = selected
        self._current_capabilities().card(self._selected_agent_id)
        self._pending_agent_id = None

    async def _record_agent_selection(self, agent_id: str) -> None:
        await self._record_agent_selection_to(self.session, agent_id)
        self._selected_agent_id = agent_id
        if self._pending_agent_id == agent_id:
            self._pending_agent_id = None

    async def _record_agent_selection_to(self, session: Session, agent_id: str) -> None:
        """把 Agent 选择写入指定会话；用于定时任务等独立会话的 Trace。"""
        card = self._current_capabilities().card(agent_id)
        await session.append_entry(CustomEntry(
            id=uuid4().hex, parent_id=session.active_leaf_id,
            timestamp=time.time(), custom_type="sec.agent_selection",
            data={"agent_id": agent_id, "digest": card.digest},
        ))

    async def _select_agent(self, agent_id: str) -> None:
        if self._closed:
            raise RuntimeError("SecClient is closed")
        if self._session_switching:
            raise RuntimeError("cannot select an agent while switching Sessions")
        async with self._resource_mutation_lock:
            self._current_capabilities().card(agent_id)
            if self.session.is_busy:
                self._pending_agent_id = agent_id
            else:
                await self._record_agent_selection(agent_id)

    async def _reload_resources(
        self, *, worker_extensions: tuple[str, ...] | None = None,
    ) -> ReloadReport:
        if self._workspace is None or self._config is None:
            raise RuntimeError("resource reload requires an initialized workspace Client")
        if self._closed:
            raise RuntimeError("SecClient is closed")
        from sec.integrations.mcp import MCPManager
        from sec.extensions.worker import WorkerExtensionManager

        async with self._reload_lock:
            if self._closed:
                raise RuntimeError("SecClient is closed")
            desired_workers = worker_extensions
            if desired_workers is None:
                desired_workers = (
                    self._worker_config_override
                    if self._worker_config_override is not None
                    else read_worker_extensions(self._workspace)
                )
            if set(desired_workers).intersection(self._enabled_extensions):
                raise ValueError("an extension cannot be enabled both in-process and in a worker")
            activation_path = self._workspace / ".sec" / "resources.json"
            legacy_path = self._workspace / ".sec" / "desktop-resources.json"
            if not activation_path.exists() and legacy_path.exists():
                activation_path = legacy_path
            resources = ResourceManager(
                self._workspace,
                activation_path=activation_path,
            )
            resources.reload(strict=True)
            catalog = AgentCatalog(self._workspace)
            catalog.reload()
            builtin = AgentCard(
                id="default", name=self._name,
                description="Default workspace assistant",
                instructions=self._instructions,
                tools=("*",), skills=("*",), mcp_servers=("*",), extensions=("*",),
                max_model_calls=self._config.agent.max_model_calls,
                max_tool_calls=self._config.agent.max_tool_calls,
            )
            if catalog.get("default") is not None:
                raise ValueError("AgentCard ID 'default' is reserved")
            mcp = MCPManager(self._workspace)
            workers = WorkerExtensionManager()
            try:
                await mcp.reload()
                await workers.reload(desired_workers)
                if self._closed:
                    raise RuntimeError("SecClient closed while resources were being prepared")
                tools = tuple(
                    replace(registered, definition=deepcopy(registered.definition))
                    for registered in (*self.agent.tools, *mcp.tools, *workers.tools)
                )
                names = [item.definition.name for item in tools]
                if len(names) != len(set(names)) or set(names).intersection(RESERVED_TOOL_NAMES):
                    raise ValueError("duplicate or reserved capability tool name")
                previous = self._capabilities
                candidate = CapabilitySnapshot(
                    1 if previous is None else previous.generation + 1,
                    (builtin, *catalog.cards), resources.snapshot.skills, tuple(tools),
                    resources.snapshot.prompts,
                )
                candidate.card(self._selected_agent_id)
                if self._pending_agent_id is not None:
                    candidate.card(self._pending_agent_id)
                before = {} if previous is None else previous.inventory()
                after = candidate.inventory()
            except BaseException as error:
                cleanup: list[BaseException] = []
                await _attempt_cleanup(cleanup, workers.aclose)
                await _attempt_cleanup(cleanup, mcp.aclose)
                if cleanup:
                    raise BaseExceptionGroup(
                        "resource preparation and candidate cleanup failed", [error, *cleanup],
                    ) from None
                raise
            self._retired_managers.extend(
                manager for manager in (self._mcp_manager, self._worker_manager)
                if manager is not None
            )
            self._mcp_manager, self._worker_manager = mcp, workers
            self._worker_extensions = desired_workers
            self._resources = resources
            self._resource_activation_path = activation_path
            self._capabilities = candidate
            self._known_capability_names.update(names)
            if not self.session.is_busy:
                await self._close_retired_managers()
            return ReloadReport(
                generation=candidate.generation,
                added=tuple(sorted(after.keys() - before.keys())),
                updated=tuple(sorted(
                    key for key in after.keys() & before.keys()
                    if after[key] != before[key] or any(
                        tool.source.startswith("worker/")
                        and key == f"tool/{tool.definition.name}"
                        for tool in candidate.tools
                    )
                )),
                removed=tuple(sorted(before.keys() - after.keys())),
                pending=("active request and its tool batch retain their original generation",)
                if self.session.is_busy else (),
                issues=tuple(str(issue) for issue in (*resources.snapshot.issues, *catalog.issues)),
            )

    async def _close_retired_managers(self) -> None:
        retired, self._retired_managers = self._retired_managers, []
        errors: list[BaseException] = []
        for manager in reversed(retired):
            await _attempt_cleanup(errors, manager.aclose)
        if errors:
            raise BaseExceptionGroup("retired capability resources failed to close", errors)

    async def _refresh_changed_capabilities(self) -> None:
        if self._mcp_manager is not None and self._mcp_manager.dirty:
            async with self._resource_mutation_lock:
                if self._mcp_manager is not None and self._mcp_manager.dirty:
                    await self._reload_resources()

    async def _finalize_capabilities(self) -> None:
        async with self._resource_mutation_lock:
            await self._close_retired_managers()

    async def _revoke_capability(self, name: str) -> None:
        if name not in self._known_capability_names:
            raise ValueError(f"unknown capability: {name!r}")
        self._revoked_capabilities.add(name)

    async def _revoke_delegation(self, agent_id: str) -> int:
        """Cancel a delegated agent and its whole subtree, without touching tool names.

        Reclamation is left to each cancelled child Trace's own ``_delegate_agent``
        finally, which cascades the cancellation downward and returns unused slices
        exactly once, so the tree unwinds deterministically.
        """
        matches = [
            record for record in self._delegations.values()
            if agent_id in record.actchain
        ]
        for record in matches:
            record.trace.cancel()
        if matches:
            await asyncio.gather(
                *(_swallow_cancelled(record.trace) for record in matches),
                return_exceptions=True,
            )
        return len(matches)

    async def _set_resource_enabled(self, kind: str, name: str, enabled: bool) -> ReloadReport:
        if kind not in {"skill", "prompt"}:
            raise ValueError(f"unsupported resource kind: {kind!r}")
        resource_kind: Literal["skill", "prompt"] = "skill" if kind == "skill" else "prompt"
        async with self._resource_mutation_lock:
            if self._workspace is None:
                raise RuntimeError("resource configuration requires a workspace")
            manager = ResourceManager(
                self._workspace, activation_path=self._resource_activation_path,
            )
            manager.reload(strict=True)
            previous = manager.is_enabled(resource_kind, name)
            before_snapshot = self._capabilities
            manager.set_enabled(resource_kind, name, enabled)
            try:
                return await self._reload_resources()
            except BaseException as error:
                if self._capabilities is not before_snapshot:
                    raise
                try:
                    manager.set_enabled(resource_kind, name, previous)
                except BaseException as rollback_error:
                    raise BaseExceptionGroup(
                        "resource update and activation rollback failed", [error, rollback_error],
                    ) from None
                raise

    async def _set_worker_extensions(self, names: Iterable[str]) -> ReloadReport:
        async with self._resource_mutation_lock:
            if self._workspace is None:
                raise RuntimeError("worker extensions require a workspace")
            previous = self._worker_extensions
            before_snapshot = self._capabilities
            desired = validate_extension_names(tuple(names))
            if set(desired).intersection(self._enabled_extensions):
                raise ValueError("an extension cannot be enabled both in-process and in a worker")
            write_worker_extensions(self._workspace, desired)
            try:
                report = await self._reload_resources(worker_extensions=desired)
                self._worker_config_override = None
                return report
            except BaseException as error:
                if self._capabilities is not before_snapshot:
                    self._worker_config_override = None
                    raise
                self._worker_extensions = previous
                try:
                    write_worker_extensions(self._workspace, previous)
                except BaseException as rollback_error:
                    raise BaseExceptionGroup(
                        "worker update and configuration rollback failed", [error, rollback_error],
                    ) from None
                raise

    async def _set_mcp_enabled(self, server_id: str, enabled: bool) -> ReloadReport:
        if not isinstance(enabled, bool):
            raise TypeError("MCP enabled must be a boolean")
        async with self._resource_mutation_lock:
            manager = self._mcp_manager
            if manager is None:
                raise RuntimeError("MCP configuration requires an initialized workspace Client")
            try:
                configuration = json.loads(manager.config_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise ValueError(f"MCP server {server_id!r} is not configured") from None
            if not isinstance(configuration, dict):
                raise ValueError("MCP configuration must be an object")
            container = "servers" if "servers" in configuration else "mcpServers"
            servers = configuration.get(container)
            if (
                not isinstance(servers, dict)
                or server_id not in servers
                or not isinstance(servers[server_id], dict)
            ):
                raise ValueError(f"MCP server {server_id!r} is not configured")
            desired = deepcopy(configuration)
            desired[container][server_id]["enabled"] = enabled
            before = self._capabilities
            manager.update_configuration(desired)
            try:
                return await self._reload_resources()
            except BaseException as error:
                if self._capabilities is not before:
                    raise
                try:
                    manager.update_configuration(configuration)
                except BaseException as rollback_error:
                    raise BaseExceptionGroup(
                        "MCP update and configuration rollback failed", [error, rollback_error],
                    ) from None
                raise

    async def _delegate_agent(
        self, agent_id: str, task: str, parent: CapabilityBinding,
    ) -> str:
        from sec._types.approvals import ToolApprovalRequest
        from sec._types.events import (
            DelegationEvent, ModelRequestStartEvent, ModelRequestEndEvent,
            ToolApprovalRequestedEvent, ToolApprovalResolvedEvent,
            ToolExecutionStartEvent, ToolExecutionEndEvent,
        )
        if self._harness is None or parent.context is None or parent.identity is None:
            raise RuntimeError("delegation requires an active parent Trace")
        if not task.strip():
            raise ValueError("delegation task must not be empty")
        ancestors = (*parent.ceiling, parent.identity)
        depth = parent.depth + 1
        if any(depth > ancestor.max_depth for ancestor in ancestors):
            raise RecoverableToolError("agent delegation depth limit reached")
        card = self._current_capabilities().card(agent_id)
        control, events = parent.context
        child_session = Session(store=self._store)
        navigation = await self._harness._hook_executor.before_navigation(
            agent_id,
            HookContext(
                trace_id=events.trace_id,
                session_id=child_session.id,
                agent_name=self.agent.name,
                ui=None,
            ),
        )
        if navigation.decline:
            return "Agent delegation was declined."
        if navigation.summary is not None:
            return navigation.summary

        delegation = parent.identity.delegation
        if delegation.fanout is not None:
            active = sum(
                1 for record in self._delegations.values() if record.parent is parent
            )
            if active >= delegation.fanout:
                raise RecoverableToolError(
                    f"agent delegation fanout limit reached ({delegation.fanout})"
                )

        sliced: TraceAllowance | None = None
        allowance = parent.allowance
        if delegation.budget_slice is not None:
            sliced = parent.allowance.carve(
                model_calls=min(
                    delegation.budget_slice, parent.allowance.remaining_model_calls
                ),
                tool_calls=min(
                    delegation.budget_slice, parent.allowance.remaining_tool_calls
                ),
            )
            allowance = sliced
        actchain = (*parent.actchain, agent_id)

        binding = CapabilityBinding(
            latest=parent.latest, agent_id=agent_id, fallback=parent.fallback,
            allowance=allowance, revoked=parent.revoked,
            delegate=self._delegate_agent, depth=depth, ceiling=ancestors, identity=card,
            refresh=self._refresh_changed_capabilities,
            actchain=actchain,
        )
        async def initialize_child() -> None:
            await child_session.append_entry(CustomEntry(
                id=uuid4().hex, parent_id=child_session.active_leaf_id,
                timestamp=time.time(), custom_type="sec.agent_selection",
                data={"agent_id": agent_id, "digest": card.digest,
                      "parent_trace_id": events.trace_id},
            ))
        binding.initialize = initialize_child
        child = self._harness.run(
            self.agent, task, session=child_session, capabilities=binding,
        )
        self._delegations[child.id] = _DelegationRecord(
            trace=child, binding=binding, actchain=actchain,
            parent=parent, sliced=sliced,
        )
        def publish(status: Literal["started", "completed", "failed", "cancelled"]) -> None:
            events.publish(DelegationEvent(
                parent_trace_id=events.trace_id, child_trace_id=child.id,
                child_session_id=child_session.id, agent_id=agent_id, status=status,
                actchain=actchain,
            ))
        publish("started")

        async def relay() -> None:
            async for event in child.events():
                if isinstance(event, ToolApprovalRequestedEvent):
                    if not isinstance(event.args, dict):
                        raise TypeError("child tool approval arguments must be an object")
                    forwarded_id = f"{child.id}:{event.tool_call_id}"
                    request = ToolApprovalRequest(
                        tool_call_id=forwarded_id, tool_name=event.tool_name,
                        capability_type="tool", source=f"delegation/{agent_id}",
                        operation=event.tool_name, mode_name=None, risk_level="unknown",
                        reason=event.reason or "Delegated tool call requires approval.",
                        arguments=event.args,
                    )
                    if not control.begin_tool_approval(request):
                        child.cancel()
                        continue
                    events.publish(replace(event, tool_call_id=forwarded_id))
                    approval = await control.wait_for_tool_approval(forwarded_id)
                    if approval is None:
                        child.cancel()
                    elif approval.outcome == "approved":
                        child.approve_tool(event.tool_call_id)
                    else:
                        child.deny_tool(event.tool_call_id, approval.reason)
                elif isinstance(event, ModelRequestStartEvent):
                    events.publish(replace(event, input={
                        **event.input,
                        "delegation": event.input.get(
                            "delegation", {"child_trace_id": child.id, "agent_id": agent_id},
                        ),
                    }))
                elif isinstance(event, (
                    ToolApprovalResolvedEvent, ToolExecutionStartEvent, ToolExecutionEndEvent,
                )):
                    events.publish(replace(
                        event, tool_call_id=f"{child.id}:{event.tool_call_id}",
                    ))
                elif isinstance(event, ModelRequestEndEvent):
                    events.publish(event)
                elif isinstance(event, DelegationEvent):
                    events.publish(event)

        relay_task = asyncio.create_task(relay())
        result_task = asyncio.create_task(child.result())
        stop_task = asyncio.create_task(control.wait_for_stop())
        try:
            done, _ = await asyncio.wait(
                {result_task, relay_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                raise asyncio.CancelledError
            if relay_task in done:
                await relay_task
            result = await result_task
            await relay_task
            if parent.metrics is not None:
                parent.metrics.merge_child(result)
            publish("completed")
            return result.text
        except asyncio.CancelledError:
            publish("cancelled")
            raise
        except Exception:
            publish("failed")
            raise
        finally:
            if not child.done:
                child.cancel()
            for pending in (relay_task, result_task, stop_task):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(relay_task, result_task, stop_task, return_exceptions=True)
            try:
                await child.result()
            except asyncio.CancelledError:
                pass
            if sliced is not None:
                parent.allowance.reclaim(sliced)
            self._delegations.pop(child.id, None)

    @property
    def extensions(self) -> ExtensionCatalog:
        """Return the contributions from extensions enabled for this Client."""
        if self._extension_catalog is None:
            raise RuntimeError("SecClient must be entered before using extensions")
        return self._extension_catalog

    @property
    def enabled_extensions(self) -> tuple[str, ...]:
        """Return the extension names currently active for this Client."""
        if self._harness is None or self._closed:
            raise RuntimeError("SecClient must be entered before using extensions")
        return self._extension_manager.enabled

    def discover_extensions(self) -> tuple[str, ...]:
        """Discover installed extension names without importing their code."""
        if self._harness is None or self._closed:
            raise RuntimeError("SecClient must be entered before using extensions")
        return self._extension_manager.discover()

    async def _set_enabled_extensions(self, enabled: Iterable[str]) -> None:
        """Replace enabled contributions while idle, preserving the current Session."""
        self._require_idle_session_switch()
        if self._config is None or self._extension_catalog is None:
            raise RuntimeError("SecClient is not fully initialized")
        desired = tuple(enabled)
        if set(desired).intersection(self._worker_extensions):
            raise ValueError("an extension cannot be enabled both in-process and in a worker")
        previous = self._extension_manager.enabled
        if set(desired) == set(previous) and len(desired) == len(previous):
            return

        storage_backend = self._config.storage.session_store_backend
        storage_owner = (
            self._extension_manager.owner_of("storage backend", storage_backend)
            if self._owns_store
            else None
        )
        if storage_owner is not None and storage_owner not in desired:
            raise ConfigError(
                f"cannot disable extension {storage_owner!r} while its SessionStore "
                "is in use; restart the Client with a different storage backend"
            )

        old_harness = self._harness
        old_agent = self._agent
        self._session_switching = True
        self._harness = None
        try:
            await old_harness.aclose()
            try:
                await self._extension_manager.set_enabled(desired)
                new_agent, new_harness = self._build_run_resources(
                    self._config,
                    self._extension_manager.catalog,
                )
                new_capabilities = self._capabilities
                if new_capabilities is not None:
                    combined_tools = (
                        *new_agent.tools,
                        *(() if self._mcp_manager is None else self._mcp_manager.tools),
                        *(() if self._worker_manager is None else self._worker_manager.tools),
                    )
                    names = [item.definition.name for item in combined_tools]
                    if len(names) != len(set(names)) or set(names).intersection(RESERVED_TOOL_NAMES):
                        await new_harness.aclose()
                        raise ValueError("duplicate or reserved capability tool name")
                    new_capabilities = replace(
                        new_capabilities, generation=new_capabilities.generation + 1,
                        tools=combined_tools,
                    )
            except BaseException as change_error:
                rollback_errors: list[BaseException] = []
                try:
                    await self._extension_manager.set_enabled(previous)
                except BaseException as rollback_error:
                    rollback_errors.append(rollback_error)
                if not rollback_errors:
                    try:
                        self._agent, self._harness = self._build_run_resources(
                            self._config,
                            self._extension_manager.catalog,
                        )
                        self._extension_catalog = self._extension_manager.catalog
                        self._enabled_extensions = previous
                        if self._capabilities is not None:
                            self._capabilities = replace(
                                self._capabilities,
                                tools=(
                                    *self._agent.tools,
                                    *(() if self._mcp_manager is None else self._mcp_manager.tools),
                                    *(() if self._worker_manager is None else self._worker_manager.tools),
                                ),
                            )
                    except BaseException as rollback_error:
                        rollback_errors.append(rollback_error)
                if rollback_errors:
                    self._agent = None
                    self._harness = None
                    raise BaseExceptionGroup(
                        "extension update and runtime rollback failed",
                        [change_error, *rollback_errors],
                    ) from change_error
                raise
            self._agent = new_agent
            self._harness = new_harness
            self._extension_catalog = self._extension_manager.catalog
            self._enabled_extensions = self._extension_manager.enabled
            self._capabilities = new_capabilities
            self._known_capability_names.update(tool.definition.name for tool in new_agent.tools)
        except BaseException:
            if self._harness is None and self._agent is None:
                self._agent = old_agent
            raise
        finally:
            self._session_switching = False

    def _build_run_resources(
        self,
        config: Config,
        catalog: ExtensionCatalog,
    ) -> tuple[Agent, Harness]:
        """Build an Agent and Harness from one stable extension catalog snapshot."""
        policy = self._policy
        if self._policy_name is not None:
            policy = catalog.policies.get(self._policy_name)
            if policy is None:
                raise ConfigError(
                    f"enabled extensions do not provide policy {self._policy_name!r}"
                )
        agent = create_agent(
            config,
            self._name,
            self._instructions,
            tools=(
                *self._tools,
                *(replace(
                    registered,
                    source=f"extension/{catalog.owner_of('tool', registered.definition.name)}",
                    workspace_scoped=False,
                ) for registered in catalog.tools),
            ),
        )
        provider = create_provider(config, providers=catalog.providers)
        small_provider = (
            build_provider(config.small_provider, config, providers=catalog.providers)
            if config.small_provider is not None
            else None
        )
        router = ModelRouter(provider, small=small_provider)
        self._router = router
        memory = create_memory_runtime(
            config,
            provider,
            self._memory_store,
            token_estimator=self._token_estimator,
            skills_provider=lambda: (
                () if self._capabilities is None else self._capabilities.skills
            ),
            small_provider=small_provider,
        )
        auxiliary = create_auxiliary_runtime(
            config,
            small_provider if small_provider is not None else provider,
        )
        harness = create_harness(
            config,
            policy=policy,
            executor=self._executor,
            token_estimator=self._token_estimator,
            providers=catalog.providers,
            hooks=catalog.hooks,
            router=router,
            auxiliary=auxiliary,
            memory=memory,
        )
        return agent, harness

    async def _handle_ui_request(
        self,
        handler_name: str,
        request: ExtensionUIRequest,
    ) -> ExtensionUIResponse:
        if self._closed:
            raise RuntimeError("SecClient is closed")
        handler = self.extensions.ui_handlers.get(handler_name)
        if handler is None:
            raise KeyError(f"no enabled extension provides UI handler {handler_name!r}")
        try:
            response = handler(request)
            if inspect.isawaitable(response):
                response = await response
            return response
        except Exception as error:
            raise SecUIHandlerError(handler_name) from error

    def _start_trace(
        self,
        prompt: str,
        *,
        session: Session | None = None,
        policy: PolicyEngine | None = None,
        background: bool = False,
        agent_id: str | None = None,
        resume_existing_turn: bool = False,
    ) -> Trace:
        """启动一轮对话并返回可观察、可控制的 Trace 句柄。

        后台运行（``background=True``）使用独立 Session 与策略，登记进
        ``_background_traces`` 而不占用 ``_active_trace``，供定时任务使用。
        """
        if self._session_switching:
            raise RuntimeError("cannot start a Trace while switching Sessions")
        if self._closed or self._harness is None:
            raise RuntimeError("SecClient must be entered before starting a Trace")
        run_session = self.session if session is None else session
        capabilities = None
        if self._capabilities is not None:
            if agent_id is not None:
                target = agent_id
            elif background:
                target = self._selected_agent_id
            else:
                target = self._pending_agent_id or self._selected_agent_id
            card = self._capabilities.card(target)
            capabilities = CapabilityBinding(
                latest=self._current_capabilities, agent_id=target, fallback=self.agent,
                allowance=TraceAllowance(
                    min(card.max_model_calls, self.agent.max_model_calls),
                    min(card.max_tool_calls, self.agent.max_tool_calls),
                ),
                revoked=self._revoked_capabilities,
                delegate=self._delegate_agent,
                identity=card,
                finalize=self._finalize_capabilities,
                refresh=self._refresh_changed_capabilities,
            )
            if session is None or session is self.session:
                async def initialize() -> None:
                    await self._record_agent_selection(target)
            else:
                async def initialize() -> None:
                    await self._record_agent_selection_to(session, target)
            capabilities.initialize = initialize
        run = self._harness.run(
            self.agent, prompt, session=run_session, capabilities=capabilities,
            policy=policy, resume_existing_turn=resume_existing_turn,
        )
        if background:
            self._background_traces[run.id] = run
        else:
            self._active_trace = run
        return run

    def _require_idle_session_switch(self) -> None:
        if self._closed or self._harness is None or self._session is None:
            raise RuntimeError("SecClient must be entered before switching Sessions")
        if self._session_switching:
            raise RuntimeError("a Session switch is already in progress")
        if self._session.is_busy or (
            self._active_trace is not None and not self._active_trace.done
        ):
            raise RuntimeError("finish or stop the active Trace before switching Sessions")

    def _require_session_store(self) -> SessionStoreProtocol:
        if self._store is None:
            raise RuntimeError("Session switching requires a SessionStore")
        return self._store

    async def _copy_session(
        self, *, include_branches: bool, session_id: str | None = None,
    ) -> Session:
        self._require_idle_session_switch()
        source_snapshot = (
            self.session.snapshot if session_id is None
            else await self.get_session_snapshot(session_id)
        )
        source_entries: tuple[SessionEntry, ...] = (
            source_snapshot.entries
            if include_branches
            else source_snapshot.active_path
        )
        return await self._copy_entries_to_new_session(
            source_entries,
            source_snapshot.active_leaf_id,
        )

    async def _copy_entries_to_new_session(
        self,
        source_entries: tuple[SessionEntry, ...],
        source_active_leaf_id: str | None,
    ) -> Session:
        self._require_idle_session_switch()
        store = self._require_session_store()
        if not source_entries:
            return await self.switch_session()
        if source_active_leaf_id is None:
            raise RuntimeError("non-empty Session snapshot has no active leaf")

        new_session_id = uuid4().hex
        id_map = {entry.id: uuid4().hex for entry in source_entries}
        copied_entries = tuple(
            replace(
                entry,
                id=id_map[entry.id],
                parent_id=(
                    None if entry.parent_id is None else id_map[entry.parent_id]
                ),
            )
            for entry in source_entries
        )
        active_leaf_id = id_map[source_active_leaf_id]
        self._session_switching = True
        try:
            await store.append(new_session_id, 0, copied_entries, active_leaf_id)
            session = await Session.restore(store, new_session_id)
            if self._capabilities is not None:
                self._restore_agent_selection(session)
            self._session = session
            self._session_id = new_session_id
            return session
        finally:
            self._session_switching = False

    async def _close(self) -> None:
        """取消尚未完成的 Trace，并关闭 Client 自行创建的资源。"""
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        if self._active_trace is not None and not self._active_trace.done:
            self._active_trace.cancel()
            try:
                await self._active_trace.result()
            except asyncio.CancelledError:
                pass
            except BaseException as error:
                errors.append(error)
        for trace in tuple(self._background_traces.values()):
            if not trace.done:
                trace.cancel()
        for trace in tuple(self._background_traces.values()):
            try:
                await trace.result()
            except asyncio.CancelledError:
                pass
            except BaseException as error:
                errors.append(error)
        self._background_traces.clear()
        async with self._resource_mutation_lock:
            if self._harness is not None:
                await _attempt_cleanup(errors, self._harness.aclose)
            await _attempt_cleanup(errors, self._close_retired_managers)
            for manager in (self._worker_manager, self._mcp_manager):
                if manager is not None:
                    await _attempt_cleanup(errors, manager.aclose)
        if self._owns_store and self._store is not None:
            close_store = getattr(self._store, "aclose", None)
            if close_store is not None:
                async def close_owned_store() -> None:
                    closed = close_store()
                    if inspect.isawaitable(closed):
                        await closed

                await _attempt_cleanup(errors, close_owned_store)
        if self._memory_store is not None:
            await _attempt_cleanup(errors, self._memory_store.aclose)
        if self._owns_embedder and self._embedder is not None:
            await _attempt_cleanup(errors, self._embedder.aclose)
        if self._owns_database and self._database is not None:
            await _attempt_cleanup(errors, self._database.aclose)
        await _attempt_cleanup(errors, self._extension_manager.aclose)
        if errors:
            raise BaseExceptionGroup("one or more SecClient resources failed to close", errors)


class AsyncSecClient(_SecClientCore):
    """异步装配单段对话，并以异步 Trace 句柄提供观测和控制能力。"""

    async def __aenter__(self) -> AsyncSecClient:
        await self._initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self._close()

    async def set_enabled_extensions(self, enabled: Iterable[str]) -> None:
        """Replace active extensions for future Runs; refuse changes during a Trace."""
        async with self._resource_mutation_lock:
            await self._set_enabled_extensions(enabled)

    async def reload_resources(self) -> ReloadReport:
        async with self._resource_mutation_lock:
            return await self._reload_resources()

    async def select_agent(self, agent_id: str) -> None:
        await self._select_agent(agent_id)

    async def set_resource_enabled(
        self, kind: str, name: str, enabled: bool,
    ) -> ReloadReport:
        return await self._set_resource_enabled(kind, name, enabled)

    async def set_worker_extensions(self, names: Iterable[str]) -> ReloadReport:
        return await self._set_worker_extensions(names)

    async def set_mcp_enabled(self, server_id: str, enabled: bool) -> ReloadReport:
        return await self._set_mcp_enabled(server_id, enabled)

    async def revoke_capability(self, name: str) -> None:
        await self._revoke_capability(name)

    async def revoke_delegation(self, agent_id: str) -> int:
        return await self._revoke_delegation(agent_id)

    def run(self, prompt: str) -> Trace:
        """启动一轮对话并返回异步 Trace 句柄。"""
        return self._start_trace(prompt)

    def regenerate(self, assistant_index: int) -> Trace:
        """重新生成第 ``assistant_index`` 条可重试助手回复，不重复追加用户提问。

        ``assistant_index`` 是活动路径中“轮末助手消息”的序号（与桌面端可重试
        回复气泡一一对应）。回退活动叶到其前置用户消息后启动一轮新 Trace，
        跳过首轮用户消息追加，从原提问之后重新生成。
        """
        self._require_idle_session_switch()
        session = self.session
        entries = session.snapshot.active_path
        retryable = _turn_final_assistants(entries)
        if assistant_index < 0 or assistant_index >= len(retryable):
            raise ValueError(
                f"assistant message {assistant_index!r} not found in the active branch"
            )
        target = retryable[assistant_index]
        target_position = entries.index(target)
        user_entry: MessageEntry | None = None
        for entry in reversed(entries[:target_position]):
            if (
                isinstance(entry, MessageEntry)
                and entry.message.role == "user"
                and entry.message.text.strip()
            ):
                user_entry = entry
                break
        if user_entry is None:
            raise ValueError("the target assistant message has no user prompt to retry")
        session.rewind_to(user_entry.id)
        return self._start_trace(
            user_entry.message.text,
            resume_existing_turn=True,
        )

    async def run_scheduled(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> Trace:
        """在独立持久 Session 中以全放行策略运行一次定时任务。

        不占用 ``_active_trace``，不影响用户当前会话；``session_id`` 对应
        该任务的专属会话（首次运行时创建，后续累积历史）。会话历史随
        SessionStore 持久化（in-memory 后端仅进程内有效）。
        """
        if self._closed or self._harness is None:
            raise RuntimeError("SecClient must be entered before running a scheduled task")
        store = self._require_session_store()
        session = (
            Session(store=store)
            if session_id is None
            else await Session.restore(store, session_id)
        )
        if self._scheduled_policy is None:
            self._scheduled_policy = AutoApprovePolicy()
        return self._start_trace(
            prompt,
            session=session,
            policy=self._scheduled_policy,
            background=True,
            agent_id=agent_id,
        )

    async def handle_ui_request(
        self,
        handler_name: str,
        request: ExtensionUIRequest,
    ) -> ExtensionUIResponse:
        """按名称调用扩展 UI handler，并等待其同步或异步响应。"""
        return await self._handle_ui_request(handler_name, request)

    async def aclose(self) -> None:
        """关闭 Client 所有的运行资源。"""
        await self._close()


from sec.apis.sync import SecClient


_TITLE_SYSTEM = (
    "You name chat sessions. Given the user's message, reply with a very short "
    "title (at most 20 characters) in the same language as the message. Reply "
    "with ONLY the title, without quotes or trailing punctuation."
)


def _turn_final_assistants(entries: tuple[SessionEntry, ...]) -> tuple[MessageEntry, ...]:
    """返回活动路径中每条“轮末助手消息”（可重试回复），按出现顺序排列。

    轮末助手消息 = 每条用户消息之后、下一条用户消息之前，最后一条有文本的
    助手消息；这与桌面端展示可重试操作栏的回复一一对应。
    """
    retryable: list[MessageEntry] = []
    last_assistant: MessageEntry | None = None
    for entry in entries:
        if isinstance(entry, MessageEntry) and entry.message.role == "user":
            if last_assistant is not None:
                retryable.append(last_assistant)
            last_assistant = None
        elif (
            isinstance(entry, MessageEntry)
            and entry.message.role == "assistant"
            and entry.message.text.strip()
        ):
            last_assistant = entry
    if last_assistant is not None:
        retryable.append(last_assistant)
    return tuple(retryable)


async def _attempt_cleanup(
    errors: list[BaseException],
    cleanup: Callable[[], Awaitable[None]],
) -> None:
    try:
        await cleanup()
    except BaseException as error:
        errors.append(error)


__all__ = [
    "AsyncSecClient",
    "SecClient",
    "create_database",
    "create_memory_store",
    "create_session_store",
]
