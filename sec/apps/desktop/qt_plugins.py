"""Prepare Qt plugin discovery on macOS."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import stat
import sys

from PySide6.QtCore import QLibraryInfo


def prepare_qt_plugins() -> None:
    if sys.platform != "darwin":
        return
    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    if not plugins.is_dir():
        raise RuntimeError(f"Qt plugin directory is missing: {plugins}")
    repaired = 0
    for path in (plugins, *plugins.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if metadata.st_flags & stat.UF_HIDDEN:
            try:
                os.chflags(path, metadata.st_flags & ~stat.UF_HIDDEN)
            except OSError as error:
                raise RuntimeError(
                    f"Could not remove the macOS hidden flag from Qt plugin path "
                    f"{path}. Check write permissions for the desktop environment."
                ) from error
            repaired += 1
    if repaired:
        logging.getLogger(__name__).warning(
            "Removed macOS hidden flags from %d Qt plugin paths in %s",
            repaired,
            plugins,
        )
