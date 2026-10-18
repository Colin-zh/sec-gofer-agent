"""Client construction, resource assembly, and application lifecycle.

This module owns the public Client implementation. The runtime and Agent domain
objects remain usable independently by internal tests and advanced integrations.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import URL, make_url

from sec.apps.extension_ui import ExtensionUIRequest, ExtensionUIResponse
from sec.agent import Agent, Run, Session
from sec.config import (
    Config,
    ConfigError,
    ContextSettings,
    ProviderName,
)
from sec._runtime.harness import Harness
from sec.extensions import ExtensionCatalog, ExtensionManager
from sec.extensions.api import ProviderFactory, StorageFactory
from sec._types.context import TokenEstimator
from sec._types.execution import ToolExecutor
from sec._types.extensions import AfterRunHook, BeforeRunHook, RunEventHook
from sec._types.policy import PolicyEngine
from sec._types.providers import ModelProvider, StreamingModelProvider
from sec._types.session import SessionEntry, SessionSnapshot, SessionSummary
from sec._types.storage import SessionStoreProtocol
from sec.storage import Database, SessionStore
from sec.exceptions import SecUIHandlerError
from sec.tools import Tool, tool


def create_provider(
    config: Config,
    *,
    providers: Mapping[str, ProviderFactory] | None = None,
) -> ModelProvider | StreamingModelProvider:
    """依据配置选择并创建流式 Provider。"""
    if providers is not None and config.provider.name in providers:
        provider = providers[config.provider.name](config)
        if not (
            callable(getattr(provider, "complete", None))
            or callable(getattr(provider, "stream", None))
        ):
            raise ConfigError(
                f"provider factory {config.provider.name!r} returned an invalid provider"
            )
        return provider
    if config.provider.name == "openai":
        from sec.providers.openai import OpenAIProvider

        return OpenAIProvider(config.provider)
    if config.provider.name == "anthropic":
        from sec.providers.anthropic import AnthropicProvider

        return AnthropicProvider(config.provider)
    raise ConfigError(f"no enabled extension provides model provider {config.provider.name!r}")


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
    if schema is None:
        return Database(database_url)

    parsed_url = database_url if isinstance(database_url, URL) else make_url(database_url)
    if parsed_url.drivername != "opengauss+asyncpg":
        raise ConfigError(
            "SEC_DATABASE_SCHEMA currently requires the opengauss+asyncpg database driver"
        )
    return Database(
        database_url,
        connect_args={"server_settings": {"search_path": schema}},
    )


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
    before_run_hooks: tuple[BeforeRunHook, ...] = (),
    after_run_hooks: tuple[AfterRunHook, ...] = (),
    run_event_hooks: tuple[RunEventHook, ...] = (),
) -> Harness:
    """创建应用 Harness；运行结束后需调用 aclose() 释放 Provider。"""
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
    return Harness(
        create_provider(config, providers=providers),
        policy=policy,
        executor=executor,
        event_buffer_size=config.runtime.event_buffer_size,
        retry_settings=config.retry,
        tool_retry_settings=config.tool_retry,
        context_settings=config.context,
        token_estimator=token_estimator,
        max_output_tokens=config.provider.max_output_tokens,
        before_run_hooks=before_run_hooks,
        after_run_hooks=after_run_hooks,
        run_event_hooks=run_event_hooks,
    )


class _SecClientCore:
    """为同步和异步 Client 共享对话资源装配与生命周期逻辑。

    构造参数中的非空配置优先于进程环境变量、dotenv 和代码默认值；Client 只
    关闭自行创建的 Provider/Database，注入的 SessionStore、Database 和 Session
    始终由调用方持有。
    """

    def __init__(
        self,
        name: str,
        instructions: str,
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
        self._harness: Harness | None = None
        self._agent: Agent | None = None
        self._active_run: Run | None = None
        self._session_switching = False
        self._enabled_extensions = tuple(enabled_extensions)
        self._extension_manager = ExtensionManager()
        self._extension_catalog: ExtensionCatalog | None = None
        self._closed = False
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
            if (config.context.context_window_tokens is None) != (
                self._token_estimator is None
            ):
                raise ConfigError(
                    "context compression requires both context_window_tokens and "
                    "a TokenEstimator"
                )
            policy = self._policy
            if self._policy_name is not None:
                policy = self._extension_catalog.policies.get(self._policy_name)
                if policy is None:
                    raise ConfigError(
                        f"enabled extensions do not provide policy {self._policy_name!r}"
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
            self._agent = create_agent(
                config,
                self._name,
                self._instructions,
                tools=(*self._tools, *self._extension_catalog.tools),
            )
            self._harness = create_harness(
                config,
                policy=policy,
                executor=self._executor,
                token_estimator=self._token_estimator,
                providers=self._extension_catalog.providers,
                before_run_hooks=self._extension_catalog.before_run_hooks,
                after_run_hooks=self._extension_catalog.after_run_hooks,
                run_event_hooks=self._extension_catalog.run_event_hooks,
            )
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
            self._session = session
            self._session_id = session.id
            return session
        finally:
            self._session_switching = False

    async def fork_session(self) -> Session:
        """在新会话中复制当前活动分支。"""
        return await self._copy_session(include_branches=False)

    async def clone_session(self) -> Session:
        """在新会话中复制完整历史树及其活动分支。"""
        return await self._copy_session(include_branches=True)

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
    def extensions(self) -> ExtensionCatalog:
        """Return the contributions from extensions enabled for this Client."""
        if self._extension_catalog is None:
            raise RuntimeError("SecClient must be entered before using extensions")
        return self._extension_catalog

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

    def _start_run(self, prompt: str) -> Run:
        """启动一轮对话并返回可观察、可控制的 Run 句柄。"""
        if self._session_switching:
            raise RuntimeError("cannot start a Run while switching Sessions")
        if self._closed or self._harness is None:
            raise RuntimeError("SecClient must be entered before starting a Run")
        run = self._harness.run(self.agent, prompt, session=self.session)
        self._active_run = run
        return run

    def _require_idle_session_switch(self) -> None:
        if self._closed or self._harness is None or self._session is None:
            raise RuntimeError("SecClient must be entered before switching Sessions")
        if self._session_switching:
            raise RuntimeError("a Session switch is already in progress")
        if self._session.is_busy or (
            self._active_run is not None and not self._active_run.done
        ):
            raise RuntimeError("finish or stop the active Run before switching Sessions")

    def _require_session_store(self) -> SessionStoreProtocol:
        if self._store is None:
            raise RuntimeError("Session switching requires a SessionStore")
        return self._store

    async def _copy_session(self, *, include_branches: bool) -> Session:
        self._require_idle_session_switch()
        source_snapshot = self.session.snapshot
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
            self._session = session
            self._session_id = new_session_id
            return session
        finally:
            self._session_switching = False

    async def _close(self) -> None:
        """取消尚未完成的 Run，并关闭 Client 自行创建的资源。"""
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        if self._active_run is not None and not self._active_run.done:
            self._active_run.cancel()
            try:
                await self._active_run.result()
            except asyncio.CancelledError:
                pass
            except BaseException as error:
                errors.append(error)
        if self._harness is not None:
            await _attempt_cleanup(errors, self._harness.aclose)
        if self._owns_store and self._store is not None:
            close_store = getattr(self._store, "aclose", None)
            if close_store is not None:
                async def close_owned_store() -> None:
                    closed = close_store()
                    if inspect.isawaitable(closed):
                        await closed

                await _attempt_cleanup(errors, close_owned_store)
        if self._owns_database and self._database is not None:
            await _attempt_cleanup(errors, self._database.aclose)
        await _attempt_cleanup(errors, self._extension_manager.aclose)
        if errors:
            raise BaseExceptionGroup("one or more SecClient resources failed to close", errors)


class AsyncSecClient(_SecClientCore):
    """异步装配单段对话，并以异步 Run 句柄提供观测和控制能力。"""

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

    def run(self, prompt: str) -> Run:
        """启动一轮对话并返回异步 Run 句柄。"""
        return self._start_run(prompt)

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
    "create_session_store",
]
