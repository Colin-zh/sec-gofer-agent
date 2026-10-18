"""组织 sec 的独立应用入口。

CLI、Desktop 和 Dashboard 各自维护应用生命周期；导入本包不会装配应用或加载可选依赖。
"""

from sec.apps.extension_ui import (
    ExtensionErrorEvent,
    ExtensionUIRequest,
    ExtensionUIResponse,
    UICancelResponse,
    UIConfirmRequest,
    UIConfirmResponse,
    UIEditorRequest,
    UIInputRequest,
    UINotifyRequest,
    UISelectRequest,
    UISetEditorTextRequest,
    UISetStatusRequest,
    UISetTitleRequest,
    UISetWidgetRequest,
    UIValueResponse,
)

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
