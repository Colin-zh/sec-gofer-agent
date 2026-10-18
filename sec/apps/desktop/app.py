"""Desktop application entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from sec.apps.desktop.appearance import application_icon
from sec.apps.desktop.qt_plugins import prepare_qt_plugins
from sec.apps.desktop.window import FloatingWindow


def main() -> int:
    parser = argparse.ArgumentParser(prog="sec-desktop")
    parser.add_argument(
        "--extension",
        action="append",
        default=[],
        help="enable an installed sec.extensions package (repeatable)",
    )
    arguments, qt_arguments = parser.parse_known_args(sys.argv[1:])
    sys.argv = [sys.argv[0], *qt_arguments]
    prepare_qt_plugins()
    app = QApplication(sys.argv)
    app.setApplicationName("Sec Desktop")
    app.setWindowIcon(application_icon())
    window = FloatingWindow(
        Path.cwd(),
        enabled_extensions=tuple(arguments.extension),
    )
    window.show()
    app.aboutToQuit.connect(window._runtime.shutdown)
    return app.exec()
