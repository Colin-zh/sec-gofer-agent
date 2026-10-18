from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from sec.apps.desktop.qt_plugins import prepare_qt_plugins


class QtPluginTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS filesystem flags")
    def test_hidden_plugins_are_repaired_preserving_other_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugins = root / "plugins"
            platforms = plugins / "platforms"
            platforms.mkdir(parents=True)
            cocoa = platforms / "libqcocoa.dylib"
            cocoa.touch()
            outside = root / "outside"
            outside.touch()
            (plugins / "link").symlink_to(outside)
            os.chflags(outside, stat.UF_HIDDEN)
            for path in (plugins, platforms, cocoa):
                os.chflags(path, stat.UF_HIDDEN | stat.UF_NODUMP)
            with patch(
                "sec.apps.desktop.qt_plugins.QLibraryInfo.path", return_value=str(plugins)
            ):
                with self.assertLogs("sec.apps.desktop.qt_plugins", level="WARNING"):
                    prepare_qt_plugins()
                for path in (plugins, platforms, cocoa):
                    self.assertFalse(path.stat().st_flags & stat.UF_HIDDEN)
                    self.assertTrue(path.stat().st_flags & stat.UF_NODUMP)
                self.assertTrue(outside.stat().st_flags & stat.UF_HIDDEN)
                with patch("sec.apps.desktop.qt_plugins.os.chflags") as change:
                    prepare_qt_plugins()
                    change.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "macOS filesystem flags")
    def test_permission_failure_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugins = Path(temporary)
            os.chflags(plugins, stat.UF_HIDDEN)
            with (
                patch("sec.apps.desktop.qt_plugins.QLibraryInfo.path", return_value=temporary),
                patch(
                    "sec.apps.desktop.qt_plugins.os.chflags",
                    side_effect=PermissionError("denied"),
                ),
                self.assertRaisesRegex(RuntimeError, "Check write permissions"),
            ):
                prepare_qt_plugins()

    def test_non_macos_does_not_modify_plugins(self) -> None:
        with (
            patch("sec.apps.desktop.qt_plugins.sys.platform", "linux"),
            patch("sec.apps.desktop.qt_plugins.QLibraryInfo.path") as path,
        ):
            prepare_qt_plugins()
            path.assert_not_called()
