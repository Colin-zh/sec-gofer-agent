"""Data contracts for application-hosted extension UI interactions."""

from __future__ import annotations

from typing import Literal, NotRequired, TypeAlias, TypedDict


class ExtensionErrorEvent(TypedDict):
    type: Literal["extension_error"]
    extensionPath: str
    event: str
    error: str


class _UIRequestBase(TypedDict):
    type: Literal["extension_ui_request"]
    id: str


class UISelectRequest(_UIRequestBase):
    method: Literal["select"]
    title: str
    options: list[str]
    timeout: NotRequired[float]


class UIConfirmRequest(_UIRequestBase):
    method: Literal["confirm"]
    title: str
    message: str
    timeout: NotRequired[float]


class UIInputRequest(_UIRequestBase):
    method: Literal["input"]
    title: str
    placeholder: NotRequired[str]
    timeout: NotRequired[float]


class UIEditorRequest(_UIRequestBase):
    method: Literal["editor"]
    title: str
    prefill: NotRequired[str]


class UINotifyRequest(_UIRequestBase):
    method: Literal["notify"]
    message: str
    notifyType: NotRequired[Literal["info", "warning", "error"]]


class UISetStatusRequest(_UIRequestBase):
    method: Literal["setStatus"]
    statusKey: str
    statusText: NotRequired[str]


class UISetWidgetRequest(_UIRequestBase):
    method: Literal["setWidget"]
    widgetKey: str
    widgetLines: NotRequired[list[str]]
    widgetPlacement: NotRequired[Literal["aboveEditor", "belowEditor"]]


class UISetTitleRequest(_UIRequestBase):
    method: Literal["setTitle"]
    title: str


class UISetEditorTextRequest(_UIRequestBase):
    method: Literal["set_editor_text"]
    text: str


ExtensionUIRequest: TypeAlias = (
    UISelectRequest
    | UIConfirmRequest
    | UIInputRequest
    | UIEditorRequest
    | UINotifyRequest
    | UISetStatusRequest
    | UISetWidgetRequest
    | UISetTitleRequest
    | UISetEditorTextRequest
)


class _UIResponseBase(TypedDict):
    type: Literal["extension_ui_response"]
    id: str


class UIValueResponse(_UIResponseBase):
    value: str


class UIConfirmResponse(_UIResponseBase):
    confirmed: bool


class UICancelResponse(_UIResponseBase):
    cancelled: Literal[True]


ExtensionUIResponse: TypeAlias = UIValueResponse | UIConfirmResponse | UICancelResponse

__all__ = [
    "ExtensionErrorEvent",
    "ExtensionUIRequest",
    "ExtensionUIResponse",
    "UICancelResponse",
    "UIConfirmRequest",
    "UIConfirmResponse",
    "UIEditorRequest",
    "UIInputRequest",
    "UINotifyRequest",
    "UISelectRequest",
    "UISetEditorTextRequest",
    "UISetStatusRequest",
    "UISetTitleRequest",
    "UISetWidgetRequest",
    "UIValueResponse",
]
