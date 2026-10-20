"""Bundled desktop branding."""

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon


ICON_PATH = Path(__file__).parent / "assets" / "icon.png"


def application_icon() -> QIcon:
    icon = QIcon(str(ICON_PATH))
    if icon.isNull():
        raise RuntimeError(f"Could not load the Sec Desktop icon: {ICON_PATH}")
    return icon


def control_icon(name: str, *, retain_disabled_color: bool = False) -> QIcon:
    path = str(ICON_PATH.parent / f"{name}.svg")
    icon = QIcon(path)
    if icon.isNull():
        raise RuntimeError(f"Could not load desktop control icon: {name}")
    if retain_disabled_color:
        icon.addFile(path, QSize(), QIcon.Mode.Disabled)
    return icon


def toggle_icon(name: str, active_name: str) -> QIcon:
    """返回普通/选中两态图标；选中态使用填充的 ``active_name`` SVG。"""
    icon = control_icon(name)
    active_path = str(ICON_PATH.parent / f"{active_name}.svg")
    icon.addFile(active_path, QSize(), QIcon.Mode.Normal, QIcon.State.On)
    return icon
