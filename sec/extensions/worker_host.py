"""Private JSON-lines subprocess host for tools-only extension workers."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import sys
from importlib.metadata import entry_points
from typing import Any

from sec.extensions.api import ExtensionAPI, ExtensionCatalog
from sec.tools.tool import ToolOutput

_VERSION = 1
_MAX_MESSAGE = 8 * 1024 * 1024


class _UnsupportedContribution(Exception):
    def __init__(self, category: str) -> None:
        self.category = category


class _ToolsOnlyAPI(ExtensionAPI):
    def __init__(self, name: str, catalog: ExtensionCatalog) -> None:
        super().__init__(name, catalog)
        self.unsupported: str | None = None

    def _reject(self, category: str) -> None:
        self.unsupported = category
        raise _UnsupportedContribution(category)

    def register_provider(self, *args: Any, **kwargs: Any) -> None:
        self._reject("provider")

    def register_policy(self, *args: Any, **kwargs: Any) -> None:
        self._reject("policy")

    def register_storage_backend(self, *args: Any, **kwargs: Any) -> None:
        self._reject("storage")

    def register_ui_handler(self, *args: Any, **kwargs: Any) -> None:
        self._reject("UI")

    def register_command(self, *args: Any, **kwargs: Any) -> None:
        self._reject("command")

    def register_hook(self, *args: Any, **kwargs: Any) -> None:
        self._reject("hook")


class _Host:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.generation: int | None = None
        self.extension: Any = None
        self.catalog = ExtensionCatalog()
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.send_lock = asyncio.Lock()

    async def send(self, request_id: int, **payload: Any) -> None:
        try:
            message = json.dumps(
                {
                    "version": _VERSION, "generation": self.generation,
                    "id": request_id, **payload,
                },
                allow_nan=False,
            ).encode() + b"\n"
            if len(message) > _MAX_MESSAGE:
                raise ValueError("message too large")
        except (TypeError, ValueError, OverflowError, RecursionError):
            message = json.dumps(
                {
                    "version": _VERSION, "generation": self.generation,
                    "id": request_id, "error": {"code": "serialization"},
                }
            ).encode() + b"\n"
        async with self.send_lock:
            self.writer.write(message)
            await self.writer.drain()

    async def initialize(self, request_id: int, params: dict[str, Any]) -> None:
        name = params["name"]
        self.generation = params["generation"]
        points = tuple(entry_points(group="sec.extensions").select(name=name))
        if len(points) != 1:
            await self.send(request_id, error={"code": "missing"})
            return
        api = _ToolsOnlyAPI(name, self.catalog)
        try:
            try:
                self.extension = points[0].load()
            except ModuleNotFoundError:
                await self.send(request_id, error={"code": "import_missing"})
                return
            except ImportError:
                await self.send(request_id, error={"code": "import_failed"})
                return
            if (
                inspect.isclass(self.extension)
                or not callable(getattr(self.extension, "activate", None))
                or not callable(getattr(self.extension, "deactivate", None))
            ):
                await self.send(request_id, error={"code": "invalid_extension"})
                return
            result = self.extension.activate(api)
            if inspect.isawaitable(result):
                await result
            if api.unsupported is not None:
                raise _UnsupportedContribution(api.unsupported)
            definitions = [
                {
                    "name": tool.definition.name,
                    "description": tool.definition.description,
                    "input_schema": dict(tool.definition.input_schema),
                    "output_schema": dict(tool.definition.output_schema),
                    "retry_safe": tool.retry_safe,
                    "risk_level": tool.risk_level,
                    "source": tool.source,
                    "workspace_scoped": tool.workspace_scoped,
                    "capability_type": tool.capability_type,
                }
                for tool in self.catalog.tools
            ]
            await self.send(request_id, result=definitions)
        except _UnsupportedContribution as error:
            await self.send(
                request_id, error={"code": "unsupported", "category": error.category}
            )
        except Exception:
            await self.send(request_id, error={"code": "activation"})
        finally:
            api._deactivate()

    async def invoke(self, request_id: int, params: dict[str, Any]) -> None:
        try:
            tool = next(
                tool for tool in self.catalog.tools
                if tool.definition.name == params["name"]
            )
            result = await tool.invoke(params["arguments"])
            if isinstance(result, ToolOutput):
                await self.send(request_id, tool_output={
                    "text": result.text,
                    "structured_content": None if result.structured_content is None else dict(result.structured_content),
                    "metadata": None if result.metadata is None else dict(result.metadata),
                    "is_error": result.is_error,
                })
            else:
                await self.send(request_id, result=result)
        except asyncio.CancelledError:
            return
        except Exception:
            await self.send(request_id, error={"code": "invocation"})

    async def close(self) -> bool:
        tasks = tuple(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.extension is not None:
            try:
                result = self.extension.deactivate()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                return False
        return True

    async def run(self, reader: asyncio.StreamReader) -> None:
        try:
            while line := await reader.readline():
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("version") != _VERSION:
                    break
                request_id = message["id"]
                method = message["method"]
                params = message["params"]
                if type(request_id) is not int or not isinstance(params, dict):
                    break
                if self.generation is None and method == "initialize":
                    if (
                        type(params.get("generation")) is not int
                        or message.get("generation") != params["generation"]
                    ):
                        break
                    await self.initialize(request_id, params)
                elif message.get("generation") != self.generation:
                    break
                elif method == "invoke":
                    if request_id in self.tasks:
                        break
                    task = asyncio.create_task(self.invoke(request_id, params))
                    self.tasks[request_id] = task
                    task.add_done_callback(
                        lambda finished, key=request_id: self.tasks.pop(key, None)
                    )
                elif method == "cancel":
                    task = self.tasks.get(params.get("id"))
                    if task is not None:
                        task.cancel()
                elif method == "shutdown":
                    success = await self.close()
                    await self.send(
                        request_id,
                        **({"result": None} if success else {"error": {"code": "deactivation"}}),
                    )
                    self.extension = None
                    return
                else:
                    break
        finally:
            await self.close()


async def _main(protocol_fd: int) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=_MAX_MESSAGE)
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    write_transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(protocol_fd, "wb", buffering=0)
    )
    writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
    try:
        await _Host(writer).run(reader)
    finally:
        read_transport.close()
        writer.close()


if __name__ == "__main__":
    # Preserve a private protocol pipe and protect fd 1 as well as Python print:
    # imports, synchronous tool threads, and plugin shutdown all use stderr.
    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    with contextlib.suppress(Exception):
        asyncio.run(_main(protocol_fd))
