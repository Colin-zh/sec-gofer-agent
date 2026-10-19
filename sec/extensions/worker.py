"""Opt-in, tools-only extension workers with atomic, versioned reloads.

Workers isolate Python imports, not permissions. Keep request snapshots' Tool
bindings until their generation is released, or close the manager at shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

from sec.tools.tool import Tool, ToolOutput

_PROTOCOL_VERSION = 1
_MAX_MESSAGE = 8 * 1024 * 1024


class WorkerExtensionError(RuntimeError):
    """A worker could not load, communicate, or execute a tool safely."""


@dataclass(frozen=True)
class WorkerExtensionStatus:
    """Public, credential-free status of a currently enabled worker."""

    name: str
    generation: int
    pid: int
    tools: tuple[str, ...]
    state: str = "ready"


class _Worker:
    def __init__(
        self,
        name: str,
        generation: int,
        process: asyncio.subprocess.Process,
        *,
        timeout: float,
        shutdown_timeout: float,
    ) -> None:
        self.name = name
        self.generation = generation
        self.process = process
        self.timeout = timeout
        self.shutdown_timeout = shutdown_timeout
        self._sequence = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._reader = asyncio.create_task(self._read(), name=f"extension:{name}:reader")

    async def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if (
                    not isinstance(message, dict)
                    or message.get("version") != _PROTOCOL_VERSION
                    or message.get("generation") != self.generation
                    or type(message.get("id")) is not int
                ):
                    raise ValueError("invalid protocol envelope")
                pending = self._pending.get(message["id"])
                if pending is None or pending.done():
                    continue
                if "error" in message:
                    error = message["error"]
                    # Only the host's fixed error vocabulary crosses this boundary.
                    code = error.get("code") if isinstance(error, dict) else None
                    category = error.get("category") if isinstance(error, dict) else None
                    if code == "unsupported" and category in {
                        "provider", "policy", "storage", "UI", "command", "hook",
                    }:
                        text = (
                            f"worker extension {self.name!r} registered unsupported "
                            f"{category} contributions; worker mode is tools-only. "
                            "Use the in-process ExtensionManager for other contributions."
                        )
                    else:
                        text = {
                            "missing": "entry point was not found or is ambiguous",
                            "invalid_extension": "entry point must export an instance with activate(api) and deactivate()",
                            "import_missing": (
                                "plugin module or one of its dependencies was not found; "
                                "check installation in the running Sec Python environment. "
                                "For editable installs on macOS, check whether the .pth file "
                                "is hidden; explicitly set the plugin source directory in "
                                "PYTHONPATH before starting Sec if necessary"
                            ),
                            "import_failed": (
                                "plugin import failed; check plugin compatibility and "
                                "dependencies in the running Sec Python environment"
                            ),
                            "activation": "activation failed",
                            "invocation": "tool invocation failed",
                            "serialization": "tool result or schema is not JSON serializable",
                            "deactivation": "deactivation failed",
                        }.get(code, "worker request failed")
                        text = f"worker extension {self.name!r}: {text}"
                    pending.set_exception(WorkerExtensionError(text))
                elif "tool_output" in message:
                    try:
                        output = message["tool_output"]
                        if not isinstance(output, dict) or set(output) != {
                            "text", "structured_content", "metadata", "is_error",
                        }:
                            raise TypeError
                        pending.set_result(ToolOutput(**output))
                    except (TypeError, ValueError):
                        pending.set_exception(WorkerExtensionError(
                            f"worker extension {self.name!r}: invalid tool output"
                        ))
                elif "result" in message:
                    pending.set_result(message["result"])
                else:
                    raise ValueError("missing result")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(
                        WorkerExtensionError(f"worker extension {self.name!r} disconnected")
                    )

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if self._closed or self._reader.done():
            raise WorkerExtensionError(f"worker extension {self.name!r} is closed")
        self._sequence += 1
        request_id = self._sequence
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            message = json.dumps(
                {
                    "version": _PROTOCOL_VERSION,
                    "generation": self.generation,
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                allow_nan=False,
            ).encode() + b"\n"
            if len(message) > _MAX_MESSAGE:
                raise WorkerExtensionError("extension worker request is too large")
            async with asyncio.timeout(self.timeout):
                async with self._send_lock:
                    if self._closed or self._reader.done():
                        raise WorkerExtensionError("extension worker is closed")
                    assert self.process.stdin is not None
                    self.process.stdin.write(message)
                    await self.process.stdin.drain()
                return await future
        except TimeoutError:
            await self._cancel(request_id)
            raise WorkerExtensionError(
                f"worker extension {self.name!r} request timed out"
            ) from None
        except asyncio.CancelledError:
            await self._cancel(request_id)
            raise
        except WorkerExtensionError:
            raise
        except Exception:
            raise WorkerExtensionError(
                f"worker extension {self.name!r} communication failed"
            ) from None
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A send failure may race with the reader reporting disconnection.
                future.exception()

    async def _cancel(self, request_id: int) -> None:
        if self._closed or self._reader.done():
            return
        message = json.dumps(
            {
                "version": _PROTOCOL_VERSION,
                "generation": self.generation,
                "id": 0,
                "method": "cancel",
                "params": {"id": request_id},
            }
        ).encode() + b"\n"
        with contextlib.suppress(Exception):
            async with asyncio.timeout(self.shutdown_timeout):
                async with self._send_lock:
                    assert self.process.stdin is not None
                    self.process.stdin.write(message)
                    await self.process.stdin.drain()

    async def aclose(self) -> None:
        if self._closed:
            return
        try:
            if self.process.returncode is None and not self._reader.done():
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(self.shutdown_timeout):
                        await self.request("shutdown", {})
        finally:
            self._closed = True
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.returncode is None:
                try:
                    async with asyncio.timeout(self.shutdown_timeout):
                        await self.process.wait()
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        self.process.terminate()
                    try:
                        async with asyncio.timeout(self.shutdown_timeout):
                            await self.process.wait()
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            self.process.kill()
                        await self.process.wait()
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)


class WorkerExtensionManager:
    """Explicitly enabled tools-only workers; reload never mutates old bindings.

    ``env`` overlays the subprocess environment (useful for installed entry-point
    paths). Timeouts cover startup and each call; close also bounds deactivation.
    Failed reloads leave enabled/tools/statuses/generation unchanged.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        shutdown_timeout: float = 2.0,
    ) -> None:
        for value in (timeout, shutdown_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("worker timeouts must be finite and positive")
        self._env = dict(env or {})
        self._timeout = timeout
        self._shutdown_timeout = shutdown_timeout
        self._tools: tuple[Tool, ...] = ()
        self._enabled: tuple[str, ...] = ()
        self._statuses: tuple[WorkerExtensionStatus, ...] = ()
        self._generation = 0
        self._generations: dict[int, tuple[_Worker, ...]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    @property
    def enabled(self) -> tuple[str, ...]:
        return self._enabled

    @property
    def statuses(self) -> tuple[WorkerExtensionStatus, ...]:
        workers = self._generations.get(self._generation, ())
        return tuple(
            replace(status, state="failed")
            if worker._reader.done() or worker.process.returncode is not None
            else status
            for status, worker in zip(self._statuses, workers, strict=True)
        )

    @property
    def generation(self) -> int:
        return self._generation

    async def reload(self, enabled: tuple[str, ...]) -> tuple[Tool, ...]:
        """Prepare every selected extension, then atomically publish its tools."""
        names = tuple(enabled)
        if any(
            not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9-]*", name) is None
            for name in names
        ):
            raise ValueError("worker extension names must be lowercase kebab-case")
        if len(set(names)) != len(names):
            raise ValueError("worker extension names must be unique")
        async with self._lock:
            if self._closed:
                raise RuntimeError("WorkerExtensionManager is closed")
            generation = self._generation + 1
            candidates: list[_Worker] = []
            tools: list[Tool] = []
            statuses: list[WorkerExtensionStatus] = []
            tool_names: set[str] = set()
            try:
                for name in names:
                    env = dict(os.environ)
                    env.update(self._env)
                    # A new interpreter alone can still read stale timestamp pycs.
                    # A unique, unwritten cache prefix forces source imports even
                    # for edits made within one second with unchanged file size.
                    env["PYTHONDONTWRITEBYTECODE"] = "1"
                    env["PYTHONPYCACHEPREFIX"] = os.path.join(
                        os.getcwd(), f".sec-worker-cache-{uuid.uuid4().hex}"
                    )
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "sec.extensions.worker_host",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=env,
                        limit=_MAX_MESSAGE,
                    )
                    worker = _Worker(
                        name, generation, process,
                        timeout=self._timeout, shutdown_timeout=self._shutdown_timeout,
                    )
                    candidates.append(worker)
                    definitions = await worker.request(
                        "initialize", {"name": name, "generation": generation}
                    )
                    if not isinstance(definitions, list):
                        raise WorkerExtensionError("invalid extension worker tool manifest")
                    registered_names: list[str] = []
                    for definition in definitions:
                        bound = self._bind(worker, name, definition)
                        tool_name = bound.definition.name
                        if tool_name in tool_names:
                            raise WorkerExtensionError(
                                f"worker tool {tool_name!r} conflicts with another extension"
                            )
                        tool_names.add(tool_name)
                        registered_names.append(tool_name)
                        tools.append(bound)
                    statuses.append(
                        WorkerExtensionStatus(
                            name, generation, process.pid, tuple(registered_names)
                        )
                    )
            except BaseException:
                await _close_workers(tuple(candidates))
                raise
            self._generations[generation] = tuple(candidates)
            self._tools = tuple(tools)
            self._enabled = names
            self._statuses = tuple(statuses)
            self._generation = generation
            return self._tools

    @staticmethod
    def _bind(worker: _Worker, extension: str, definition: dict[str, Any]) -> Tool:
        async def invoke(arguments: dict[str, Any]) -> Any:
            return await worker.request(
                "invoke", {"name": definition["name"], "arguments": arguments}
            )

        try:
            # Plugin-declared provenance is untrusted; core scopes by worker/<id>.
            return Tool.from_schema(
                invoke,
                name=definition["name"],
                description=definition["description"],
                input_schema=definition["input_schema"],
                output_schema=definition["output_schema"],
                retry_safe=definition["retry_safe"],
                risk_level=definition["risk_level"],
                source=f"worker/{extension}",
                workspace_scoped=definition["workspace_scoped"],
                capability_type="tool",
            )
        except Exception:
            raise WorkerExtensionError("invalid extension worker tool manifest") from None

    async def release_generation(self, generation: int) -> None:
        """Close an obsolete generation after all its request snapshots retire."""
        async with self._lock:
            if generation == self._generation and not self._closed:
                raise ValueError("cannot release the currently published generation")
            workers = self._generations.pop(generation, ())
            await _close_workers(workers)

    async def aclose(self) -> None:
        """Close all generations, including obsolete snapshot bindings."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = tuple(
                worker for generation in self._generations.values() for worker in generation
            )
            self._generations.clear()
            self._tools = ()
            self._enabled = ()
            self._statuses = ()
            await _close_workers(workers)


async def _close_workers(workers: tuple[_Worker, ...]) -> None:
    # Shield cleanup so cancelling a reload/close cannot orphan subprocesses.
    cleanup = asyncio.gather(*(worker.aclose() for worker in workers), return_exceptions=True)
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError
