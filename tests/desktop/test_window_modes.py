from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from sec.apps.desktop.appearance import ICON_PATH, application_icon
from sec.apps.desktop.runtime import SecRuntime
from sec.apps.desktop.window import FloatingWindow


class WindowModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = QSettings(
            str(Path(self.directory.name) / "settings.ini"),
            QSettings.Format.IniFormat,
        )
        settings_patch = patch("sec.apps.desktop.window.QSettings", return_value=self.settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        start_patch = patch.object(SecRuntime, "start")
        self.start = start_patch.start()
        self.addCleanup(start_patch.stop)
        shutdown_patch = patch.object(SecRuntime, "shutdown")
        self.shutdown = shutdown_patch.start()
        self.addCleanup(shutdown_patch.stop)
        self.window = FloatingWindow(Path(self.directory.name))
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.window.close)

    def test_icon_is_bundled_and_used_by_both_windows(self) -> None:
        self.assertTrue(ICON_PATH.is_file())
        self.assertFalse(application_icon().pixmap(32, 32).isNull())
        self.assertFalse(self.window.windowIcon().isNull())
        self.assertFalse(self.window._ball.windowIcon().isNull())

    def test_ball_paints_without_qt_callback_errors(self) -> None:
        with patch("sys.excepthook") as exception_hook:
            self.window.show_ball_mode()
            self.app.processEvents()
            image = self.window._ball.grab().toImage()
        exception_hook.assert_not_called()
        self.assertFalse(image.isNull())
        self.assertGreater(image.pixelColor(36, 36).alpha(), 0)
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

    def test_switch_preserves_session_draft_geometry_and_runtime(self) -> None:
        self.window._set_session_id("session-under-test")
        self.window._set_session_history((("user", "hello"),))
        self.window._input.setPlainText("unfinished prompt")
        self.window._update_status("正在运行")
        geometry = self.window.saveGeometry()
        runtime = self.window._runtime

        QTest.mouseClick(self.window._compact, Qt.MouseButton.LeftButton)
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.window._ball.isVisible())
        self.assertTrue(
            self.window._ball.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        )
        QTest.mouseClick(self.window._ball, Qt.MouseButton.LeftButton)

        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window._ball.isVisible())
        self.assertEqual(self.window.saveGeometry(), geometry)
        self.assertEqual(self.window._session_id, "session-under-test")
        self.assertEqual(self.window._messages, [("user", "hello")])
        self.assertEqual(self.window._input.toPlainText(), "unfinished prompt")
        self.assertEqual(self.window._status.text(), "正在运行")
        self.assertIs(self.window._runtime, runtime)
        self.start.assert_called_once()
        self.shutdown.assert_not_called()

    def test_drag_moves_ball_without_restoring_and_remembers_position(self) -> None:
        self.window.show_ball_mode()
        ball = self.window._ball
        area = ball.screen().availableGeometry()
        ball.move_on_screen(area.topLeft() + QPoint(80, 80))
        original = ball.pos()
        local = QPointF(36, 36)
        origin = QPointF(ball.mapToGlobal(QPoint(36, 36)))
        for event_type, position, button, buttons in (
            (QMouseEvent.Type.MouseButtonPress, origin,
             Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
            (QMouseEvent.Type.MouseMove, origin + QPointF(90, 60),
             Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
            (QMouseEvent.Type.MouseButtonRelease, origin + QPointF(90, 60),
             Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
        ):
            event = QMouseEvent(
                event_type, local, position, button, buttons,
                Qt.KeyboardModifier.NoModifier,
            )
            self.app.sendEvent(ball, event)
        self.assertEqual(ball.pos(), original + QPoint(90, 60))
        self.assertTrue(ball.isVisible())
        self.assertFalse(self.window.isVisible())
        self.window.show_focus_mode()
        self.window.show_ball_mode()
        self.assertEqual(ball.pos(), original + QPoint(90, 60))
        ball.move_on_screen(QPoint(-100000, -100000))
        self.assertTrue(ball.screen().availableGeometry().contains(ball.geometry()))

    def test_approval_restores_focus_and_resolves_the_same_runtime(self) -> None:
        self.window.show_ball_mode()
        with (
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.No
            ),
            patch.object(self.window._runtime, "resolve_approval") as resolve,
        ):
            self.window._approve_tool("tool-1", "bash", "{}")
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window._ball.isVisible())
        resolve.assert_called_once_with("tool-1", False)

    def test_quit_from_ball_closes_windows_and_stops_runtime(self) -> None:
        self.window.show_ball_mode()
        position = self.window._ball.pos()
        with patch.object(QApplication, "quit") as quit_app:
            self.window._ball.quit_requested.emit()
        quit_app.assert_called_once()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window._ball.isVisible())
        self.shutdown.assert_called_once()
        self.assertEqual(self.settings.value("ball/position"), position)


if __name__ == "__main__":
    unittest.main()
