"""Bundled desktop branding."""

from pathlib import Path

from PySide6.QtGui import QIcon


ICON_PATH = Path(__file__).parent / "assets" / "icon.png"


def application_icon() -> QIcon:
    icon = QIcon(str(ICON_PATH))
    if icon.isNull():
        raise RuntimeError(f"Could not load the Sec Desktop icon: {ICON_PATH}")
    return icon
