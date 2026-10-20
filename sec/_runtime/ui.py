"""Extension→host 阻塞 UI 通道的运行时实现。

扩展在 hook 内通过 HookContext.ui 调用 select/confirm/input/editor/notify，
请求经事件 + future 投递给宿主（CLI/Desktop）渲染并回传，实现阻塞等待。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from sec.apps.extension_ui import (
    ExtensionUIRequest,
    ExtensionUIResponse,
    UIConfirmRequest,
    UIEditorRequest,
    UIInputRequest,
    UINotifyRequest,
    UISelectRequest,
)
from sec._runtime.control import TraceControl
from sec._runtime.event_stream import TraceEventStream
from sec._types.events import ExtensionUIRequestedEvent, ExtensionUIResolvedEvent
from sec._types.hooks import BlockingUI


class HostBlockingUI(BlockingUI):
    """通过 TraceControl 的 pending-future 通道阻塞等待宿主回传 UI 响应。"""

    def __init__(
        self,
        control: TraceControl,
        events: TraceEventStream,
        extension_name: str,
    ) -> None:
        self._control = control
        self._events = events
        self._extension_name = extension_name

    async def _request(
        self,
        request: ExtensionUIRequest,
        *,
        timeout: float | None = None,
    ) -> ExtensionUIResponse | None:
        request_id = request["id"]
        if not self._control.begin_ui_request(request_id, request):
            return None
        self._events.publish(
            ExtensionUIRequestedEvent(
                request_id=request_id,
                extension_name=self._extension_name,
                request=request,
            )
        )
        response_task = asyncio.create_task(
            self._control.wait_for_ui_response(request_id)
        )
        try:
            if timeout is None:
                response = await response_task
            else:
                response = await asyncio.wait_for(response_task, timeout)
        except asyncio.TimeoutError:
            response = None
        finally:
            if not response_task.done():
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)

        if response is None:
            self._events.publish(
                ExtensionUIResolvedEvent(
                    request_id=request_id,
                    outcome="cancelled",
                )
            )
        else:
            self._events.publish(
                ExtensionUIResolvedEvent(
                    request_id=request_id,
                    outcome="resolved",
                    response=response,
                )
            )
        return response

    async def select(
        self,
        title: str,
        options: list[str],
        *,
        timeout: float | None = None,
    ) -> str | None:
        request: UISelectRequest = {
            "type": "extension_ui_request",
            "id": uuid4().hex,
            "method": "select",
            "title": title,
            "options": options,
            **({"timeout": timeout} if timeout is not None else {}),
        }
        response = await self._request(request, timeout=timeout)
        if response is None or response.get("cancelled"):
            return None
        return response.get("value")

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool | None:
        request: UIConfirmRequest = {
            "type": "extension_ui_request",
            "id": uuid4().hex,
            "method": "confirm",
            "title": title,
            "message": message,
            **({"timeout": timeout} if timeout is not None else {}),
        }
        response = await self._request(request, timeout=timeout)
        if response is None or response.get("cancelled"):
            return None
        return response.get("confirmed")

    async def input(
        self,
        title: str,
        *,
        placeholder: str | None = None,
        timeout: float | None = None,
    ) -> str | None:
        request: UIInputRequest = {
            "type": "extension_ui_request",
            "id": uuid4().hex,
            "method": "input",
            "title": title,
            **({"placeholder": placeholder} if placeholder is not None else {}),
            **({"timeout": timeout} if timeout is not None else {}),
        }
        response = await self._request(request, timeout=timeout)
        if response is None or response.get("cancelled"):
            return None
        return response.get("value")

    async def editor(
        self,
        title: str,
        *,
        prefill: str | None = None,
    ) -> str | None:
        request: UIEditorRequest = {
            "type": "extension_ui_request",
            "id": uuid4().hex,
            "method": "editor",
            "title": title,
            **({"prefill": prefill} if prefill is not None else {}),
        }
        response = await self._request(request)
        if response is None or response.get("cancelled"):
            return None
        return response.get("value")

    async def notify(
        self,
        message: str,
        *,
        notify_type: str | None = None,
    ) -> None:
        request: UINotifyRequest = {
            "type": "extension_ui_request",
            "id": uuid4().hex,
            "method": "notify",
            "message": message,
            **({"notifyType": notify_type} if notify_type is not None else {}),
        }
        if self._control.stop_requested:
            return
        self._events.publish(
            ExtensionUIRequestedEvent(
                request_id=request["id"],
                extension_name=self._extension_name,
                request=request,
            )
        )


__all__ = ["HostBlockingUI"]
