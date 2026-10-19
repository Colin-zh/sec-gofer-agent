from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QIcon, QMouseEvent, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QListView, QPushButton

from sec.apps.desktop.appearance import ICON_PATH, application_icon
from sec.apps.desktop.runtime import SecRuntime
from sec.apps.desktop.window import FloatingWindow
from sec.apps.desktop.tool_page import ToolEntry, ToolInventory


class WindowModeTests(unittest.TestCase):
    def test_sidebar_toggle_preserves_chat_and_restores_width(self) -> None:
        self.window._input.setPlainText("draft")
        self.window._set_session_history((("user", "hello"),))
        widths = self.window._main_splitter.sizes()
        self.assertFalse(self.window._sidebar_toggle.isChecked())
        self.window._sidebar_toggle.click()
        self.app.processEvents()
        self.assertTrue(self.window._sidebar.isHidden())
        self.assertTrue(self.window._sidebar_toggle.isChecked())
        self.assertTrue(self.window._sidebar_toggle.isVisible())
        self.assertEqual(self.window._sidebar_toggle.toolTip(), "展开侧边栏")
        image = self.window._sidebar_toggle.grab().toImage()
        self.assertEqual(image.pixelColor(image.width() // 2, 3), QColor("#dfe7f2"))
        self.window._sidebar_toggle.click()
        self.app.processEvents()
        self.assertFalse(self.window._sidebar.isHidden())
        self.assertFalse(self.window._sidebar_toggle.isChecked())
        image = self.window._sidebar_toggle.grab().toImage()
        self.assertNotEqual(image.pixelColor(image.width() // 2, 3), QColor("#dfe7f2"))
        self.assertEqual(self.window._main_splitter.sizes(), widths)
        self.assertEqual(self.window._input.toPlainText(), "draft")
        self.assertEqual(self.window._messages, [("user", "hello")])

    def test_pin_icon_is_centered_in_both_states(self) -> None:
        button = self.window._pin
        for checked in (False, True):
            with self.subTest(checked=checked):
                button.setChecked(checked)
                self.app.processEvents()
                image = button.grab().toImage()
                pixels = [
                    (x, y)
                    for y in range(image.height())
                    for x in range(image.width())
                    if image.pixelColor(x, y).lightness() < 100
                ]
                self.assertTrue(pixels)
                for axis, dimension in ((0, image.width()), (1, image.height())):
                    center = (
                        min(pixel[axis] for pixel in pixels)
                        + max(pixel[axis] for pixel in pixels)
                    ) / 2
                    self.assertAlmostEqual(center, (dimension - 1) / 2, delta=1)
                if checked:
                    self.assertEqual(
                        image.pixelColor(image.width() // 2, 3), QColor("#dfe7f2")
                    )

    def test_composer_controls_are_ordered_and_evenly_aligned(self) -> None:
        from PySide6.QtWidgets import QPushButton
        buttons = [
            next(button for button in self.window._composer.findChildren(QPushButton)
                 if button.text() == label)
            for label in ("help", "agent", "mode · 普通", "reload")
        ]
        self.assertFalse(self.window._agent_button.isEnabled())
        self.assertFalse(self.window._agent_button.icon().pixmap(16, 16).isNull())
        self.assertIn("下一次 Run", self.window._agent_button.toolTip())
        self.window._runtime_ready("workspace")
        self.assertTrue(self.window._agent_button.isEnabled())
        for button in buttons:
            self.assertEqual(button.height(), 36)
            self.assertEqual(button.geometry().center().y(), buttons[0].geometry().center().y())
            self.assertEqual(
                button.width(),
                8 + 16 + 6 + button.fontMetrics().horizontalAdvance(button.text()),
            )
        gaps = [
            right.geometry().left() - left.geometry().right() - 1
            for left, right in zip(buttons, buttons[1:])
        ]
        self.assertEqual(gaps, [16, 16, 16])
        for button in (self.window._agent_button, self.window._scheduled_tasks):
            normal = button.icon().pixmap(16, 16, QIcon.Mode.Normal).toImage()
            disabled = button.icon().pixmap(16, 16, QIcon.Mode.Disabled).toImage()
            self.assertFalse(normal.isNull())
            self.assertFalse(disabled.isNull())

            def visible_colors(image):
                return {
                    image.pixelColor(x, y).getRgb()[:3]
                    for x in range(image.width())
                    for y in range(image.height())
                    if image.pixelColor(x, y).alpha() > 0
                }

            # SVG mode caches can use different edge antialiasing; compare source colors.
            self.assertEqual(
                visible_colors(normal),
                visible_colors(disabled),
                button.text(),
            )

    def test_scheduled_tasks_is_reserved_between_commands_and_tools(self) -> None:
        from PySide6.QtWidgets import QPushButton
        button = self.window._scheduled_tasks
        self.assertEqual(button.text(), "定时任务")
        self.assertFalse(button.isEnabled())
        self.assertFalse(button.icon().pixmap(16, 16).isNull())
        self.assertIn("暂未开放", button.toolTip())
        layout = button.parentWidget().layout()
        commands = next(
            item for item in self.window.findChildren(QPushButton)
            if item.text() == "快捷指令"
        )
        self.assertEqual(layout.indexOf(button), layout.indexOf(commands) + 1)
        self.assertEqual(layout.indexOf(self.window._memory_button), layout.indexOf(button) + 1)
        self.assertEqual(layout.indexOf(self.window._tools_button), layout.indexOf(button) + 2)

    def test_memory_placeholder_preserves_conversation_and_draft(self) -> None:
        self.window._input.setPlainText("draft")
        self.window._set_session_history((("user", "hello"),))
        self.window._memory_button.click()
        self.assertIs(self.window._pages.currentWidget(), self.window._memory_page)
        self.assertFalse(self.window._memory_button.icon().pixmap(16, 16).isNull())
        self.window._show_chat()
        self.assertEqual(self.window._input.toPlainText(), "draft")
        self.assertEqual(self.window._messages, [("user", "hello")])

    def test_observation_entry_preserves_conversation_and_draft(self) -> None:
        self.assertEqual(self.window._trace_button.text(), "")
        self.assertFalse(self.window._trace_button.icon().pixmap(22, 22).isNull())
        self.assertEqual(self.window._trace_button.width(), 36)
        self.assertEqual(self.window._trace_button.accessibleName(), "打开执行 Trace")
        self.assertIn("观测", self.window._trace_button.toolTip())
        self.window._input.setPlainText("unfinished trace draft")
        self.window._set_session_history((("user", "hello"),))
        self.window._trace_button.click()
        self.assertIs(self.window._pages.currentWidget(), self.window._trace_page)
        self.assertTrue(self.window._trace_button.isChecked())
        self.assertEqual(self.window._trace_button.accessibleName(), "关闭执行 Trace")
        self.assertFalse(any(
            button.text() == "返回对话"
            for button in self.window._trace_page.findChildren(QPushButton)
        ))
        self.assertEqual(self.window._input.toPlainText(), "unfinished trace draft")
        self.assertEqual(self.window._messages, [("user", "hello")])
        self.window._trace_button.click()
        self.assertIs(self.window._pages.currentWidget(), self.window._chat_page)
        self.assertFalse(self.window._trace_button.isChecked())
        self.assertEqual(self.window._trace_button.accessibleName(), "打开执行 Trace")
        self.assertEqual(self.window._input.toPlainText(), "unfinished trace draft")

    def test_trace_state_tracks_session_selection_and_other_pages(self) -> None:
        self.window._set_session_id("current")
        self.window._set_sessions((("current", "Current", None), ("other", "Other", None)))
        with patch.object(self.window._runtime, "session_action") as dispatch:
            for session_id in ("current", "other"):
                self.window._trace_button.click()
                self.assertTrue(self.window._trace_button.isChecked())
                item = next(
                    self.window._sessions.item(index)
                    for index in range(self.window._sessions.count())
                    if self.window._sessions.item(index).data(Qt.ItemDataRole.UserRole)
                    == session_id
                )
                QTest.mouseClick(
                    self.window._sessions.viewport(), Qt.MouseButton.LeftButton,
                    pos=self.window._sessions.visualItemRect(item).center(),
                )
                self.assertIs(self.window._pages.currentWidget(), self.window._chat_page)
                self.assertFalse(self.window._trace_button.isChecked())
            dispatch.assert_called_once_with("open", "other")
        self.window._show_trace()
        self.assertTrue(self.window._trace_button.isChecked())
        self.window._show_commands()
        self.assertFalse(self.window._trace_button.isChecked())

    def test_status_is_inline_after_session_id_not_in_header_actions(self) -> None:
        self.window._set_session_id("current-session")
        self.window._update_status("正在运行")
        self.app.processEvents()
        caption = self.window._session_caption.geometry()
        dot = self.window._status_dot.geometry()
        status = self.window._status.geometry()
        trace = self.window._trace_button.geometry()
        compact = self.window._compact.geometry()
        self.assertLess(caption.right(), dot.left())
        self.assertLess(dot.right(), status.left())
        self.assertAlmostEqual(caption.center().y(), dot.center().y(), delta=1)
        self.assertAlmostEqual(caption.center().y(), status.center().y(), delta=1)
        self.assertLess(status.right(), trace.left())
        self.assertLess(trace.right(), compact.left())
        self.assertIn("#e8a317", self.window._status_dot.styleSheet())

    def test_help_entries_open_reference_without_dispatching_or_changing_chat(self) -> None:
        from PySide6.QtWidgets import QLabel, QPushButton
        self.window._set_session_history((("user", "Existing conversation"),))
        self.window._input.setPlainText("Retained draft")
        self.window._set_command_completions((
            ("/new", "Start a Session"), ("/steer", "Guide current Run"),
            ("/skill:review", "Review code"),
        ))
        with patch.object(self.window._runtime, "submit_input") as dispatch:
            for text in ("快捷指令", "help", "查看可用命令"):
                button = next(b for b in self.window.findChildren(QPushButton) if b.text() == text)
                button.click()
                self.assertIs(self.window._pages.currentWidget(), self.window._commands_page)
                self.window._show_chat()
        dispatch.assert_not_called()
        self.assertEqual(self.window._input.toPlainText(), "Retained draft")
        self.assertEqual(self.window._messages, [("user", "Existing conversation")])
        page = self.window._commands_page
        page.search.setText("review")
        labels = [
            label.text()
            for index in range(page._cards.count())
            if page._cards.itemAt(index).widget() is not None
            for label in page._cards.itemAt(index).widget().findChildren(QLabel)
        ]
        self.assertIn("/skill:review", labels)
        self.assertNotIn("/new", labels)
        self.window._input.clear()
        page.command_selected.emit("/skill:review")
        self.assertEqual(self.window._input.toPlainText(), "/skill:review ")
        self.assertIs(self.window._pages.currentWidget(), self.window._chat_page)
        with patch.object(self.window._runtime, "submit_message") as submit:
            self.window._input.setPlainText("/help")
            self.window._send_prompt()
        submit.assert_not_called()
        self.assertIs(self.window._pages.currentWidget(), page)

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

    def test_empty_session_restores_initial_welcome_without_permission_suggestion(self) -> None:
        labels = [button.text() for button in self.window._welcome.findChildren(QPushButton)]
        self.assertEqual(labels, ["查看可用命令", "重新加载资源"])
        self.window._set_session_history((("user", "previous conversation"),))
        self.assertTrue(self.window._welcome.isHidden())
        self.window._set_session_id("new-session")
        self.window._show_command_result("Started new Session new-session.", "info")
        self.window._set_session_history(())
        self.assertFalse(self.window._welcome.isHidden())
        self.assertEqual(self.window._messages, [])
        self.assertEqual(self.window._title.text(), "新对话")

    def test_active_svg_assets_retained_and_obsolete_assets_removed(self) -> None:
        used = {
            "help", "components", "new", "send", "refresh", "reload",
            "stop", "pin", "mode", "instructions", "compact", "more", "apply", "timer",
        }
        bundled = {path.stem for path in ICON_PATH.parent.glob("*.svg")}
        self.assertTrue(used <= bundled)
        self.assertFalse(bundled & {"commands", "skills", "prompts", "extensions", "edit"})

    def test_tools_submenu_starts_hidden_and_toggles_without_navigation(self) -> None:
        tree = self.window._resources
        button = self.window._tools_button
        self.assertTrue(tree.isHidden())
        self.assertFalse(button.isChecked())
        self.assertFalse(self.window._tools_more.pixmap().isNull())
        self.window._input.setPlainText("retained draft")
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(tree.isVisible())
        self.assertIs(self.window._pages.currentWidget(), self.window._chat_page)
        self.assertIn("padding: 4px 0 4px 22px", tree.styleSheet())
        self.assertIn("background: #e9edf2", tree.styleSheet())
        self.assertGreater(self.window._tools_more.x(), button.width() // 2)
        self.window._set_resources(ToolInventory())
        self.assertTrue(tree.isVisible())
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertTrue(tree.isHidden())
        self.assertEqual(self.window._input.toPlainText(), "retained draft")

    def test_permission_selector_uses_styled_popup_and_preserves_modes(self) -> None:
        page = self.window._tool_page
        self.window._runtime_ready("workspace")
        self.window._open_tools("skill")
        combo = page._mode
        self.assertIsInstance(combo.view(), QListView)
        self.assertEqual([combo.itemData(i) for i in range(combo.count())], ["normal", "plan", "auto"])
        self.assertIn("QComboBox:disabled", page.styleSheet())
        self.assertIn("QListView::item:selected", combo.view().styleSheet())
        with patch("sys.excepthook") as exception_hook:
            combo.showPopup()
            self.app.processEvents()
            self.assertTrue(combo.view().isVisible())
            bounds = [combo.view().visualRect(combo.model().index(i, 0)) for i in range(combo.count())]
            self.assertTrue(all(rect.height() >= 36 for rect in bounds))
            self.assertTrue(all(left.bottom() < right.top() for left, right in zip(bounds, bounds[1:])))
            self.assertFalse(combo.grab().isNull())
            self.assertFalse(combo.view().grab().isNull())
            combo.hidePopup()
        exception_hook.assert_not_called()

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
        self.window._transcript.approval_decided.disconnect()
        decisions = []
        self.window._transcript.approval_decided.connect(lambda *args: decisions.append(args))
        self.window._approve_tool("tool-1", "bash", "{}")
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window._ball.isVisible())
        from sec.apps.desktop.conversation import ApprovalCard
        card = self.window._transcript.findChild(ApprovalCard)
        self.assertIsNotNone(card)
        card.reject.click()
        self.assertEqual(decisions, [("tool-1", False)])
        self.assertFalse(card.allow.isEnabled())
        self.assertFalse(card.reject.isEnabled())
        self.window._set_session_history((("notice", "审批拒绝 · bash"),))
        self.app.processEvents()
        self.assertFalse(card.isVisible())
        self.assertEqual(self.window._transcript.bubbles[0].text, "审批拒绝 · bash")

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

    def test_history_and_stream_use_role_specific_bubbles(self) -> None:
        self.window._set_session_history((("user", "<literal>"), ("assistant", "**reply**")))
        self.app.processEvents()
        user, agent = self.window._transcript.bubbles
        self.assertEqual(user.role, "user")
        self.assertEqual(agent.role, "assistant")
        self.assertEqual(user.body.toPlainText(), "<literal>")
        self.assertEqual(agent.body.toPlainText(), "reply")
        self.assertIn("#e9edf3", user.styleSheet())
        self.assertIn("transparent", agent.styleSheet())
        self.assertIn("#000", user.body.styleSheet())
        self.assertIn("#000", agent.body.styleSheet())
        user_x = user.mapTo(self.window._transcript, QPoint()).x()
        agent_x = agent.mapTo(self.window._transcript, QPoint()).x()
        self.assertGreater(user_x, agent_x)

        self.window._assistant_started()
        self.window._append_delta("first ")
        self.window._messages.append(("user", "queued follow-up"))
        self.window._append_user_message("queued follow-up")
        self.window._show_command_result("queued", "info")
        self.window._append_delta("second")
        self.assertEqual(self.window._messages[-2], ("assistant", "first second"))
        self.assertEqual(self.window._messages[-1], ("user", "queued follow-up"))
        self.assertEqual(self.window._assistant_bubble.text, "first second")
        self.window._run_finished()
        self.assertEqual(self.window._transcript.bubbles[-1].text, "queued")

    def test_status_icons_session_id_and_compact_composer(self) -> None:
        styles = []
        for status in ("就绪", "正在运行", "已完成"):
            self.window._update_status(status)
            styles.append(self.window._status_dot.styleSheet())
            self.assertEqual(self.window._status.text(), status)
        self.assertEqual(len(set(styles)), 3)
        self.assertEqual(self.window._compact.text(), "")
        self.assertFalse(self.window._compact.icon().isNull())
        self.assertIn("悬浮球", self.window._compact.toolTip())
        session_id = "1234567890abcdef1234567890abcdef"
        self.window._set_session_id(session_id)
        self.assertIn(session_id, self.window._session_caption.text())
        self.assertIn("Enter 引导", self.window._input.placeholderText())
        self.assertIn("Enter 追加", self.window._input.placeholderText())
        self.assertNotIn("/steer", self.window._input.placeholderText())
        self.assertLess(self.window._input.height(), 100)

    def test_completion_shows_description_but_inserts_only_command(self) -> None:
        editor = self.window._input
        editor.set_commands((("/help", "Show commands"), ("/followup", "Queue input")))
        editor.setPlainText("/he")
        editor.moveCursor(editor.textCursor().MoveOperation.End)
        editor._update_completion()
        model = editor._completer.completionModel()
        self.assertIn("Show commands", model.index(0, 0).data())
        self.assertEqual(editor._completer.currentCompletion(), "/help")
        editor._completer.activated[str].emit(editor._completer.currentCompletion())
        self.assertEqual(editor.toPlainText(), "/help ")

    def test_resources_and_rename_target_selected_session(self) -> None:
        self.window._set_resources(ToolInventory((
            ToolEntry("skill", "review", "Review code", "Review content", True),
            ToolEntry("prompt", "daily", "Daily prompt", "Daily content", True),
            ToolEntry("extension", "hello", "Enabled", "Extension", True),
        )))
        tree = self.window._resources
        self.assertEqual(tree.topLevelItemCount(), 5)
        self.assertEqual(tree.topLevelItem(2).data(1, Qt.ItemDataRole.UserRole), "1")
        self.window._open_tools("skill")
        self.assertIn("Review content", self.window._tool_page._preview.toPlainText())
        self.window._set_session_id("current")
        self.window._set_sessions((("other", "Old", None),))
        with (
            patch("sec.apps.desktop.window.rename_session", return_value=" New name "),
            patch.object(self.window._runtime, "rename_session") as rename,
        ):
            self.window._rename_session(self.window._sessions.item(0))
        rename.assert_called_once_with("other", "New name")
        self.assertEqual(self.window._session_id, "current")

    def test_resource_navigation_is_text_only_and_preserves_chat(self) -> None:
        inventory = ToolInventory((
            ToolEntry("skill", "review", "Review code", "Review content", False),
        ))
        self.window._set_resources(inventory)
        tree = self.window._resources
        self.assertFalse(tree.rootIsDecorated())
        self.assertEqual(tree.indentation(), 0)
        self.app.processEvents()
        self.assertFalse(tree.horizontalScrollBar().isVisible())
        for index, title in enumerate(("智能体", "提示词模板", "技能", "插件", "MCP")):
            item = tree.topLevelItem(index)
            self.assertEqual(item.text(0), title)
            self.assertTrue(item.icon(0).isNull())
            self.assertEqual(item.childCount(), 0)
            count = self.window._resource_counts[item.data(0, Qt.ItemDataRole.UserRole)]
            self.assertTrue(count.alignment() & Qt.AlignmentFlag.AlignBottom)
            self.assertIn("#9299a3", count.styleSheet())
        self.window._input.setPlainText("draft")
        self.window._set_session_history((("user", "hello"),))
        tree.itemClicked.emit(tree.topLevelItem(2), 0)
        self.assertIs(self.window._pages.currentWidget(), self.window._tool_page)
        self.assertIn("Review content", self.window._tool_page._preview.toPlainText())
        self.app.processEvents()
        self.assertEqual(
            self.window._tool_page._explanation.palette().color(QPalette.ColorRole.WindowText).name(),
            "#202124",
        )
        self.assertEqual(tree.topLevelItem(2).data(1, Qt.ItemDataRole.UserRole), "1")
        self.window._set_resources(inventory)
        self.window._show_chat()
        self.assertEqual(self.window._input.toPlainText(), "draft")
        self.assertEqual(self.window._messages, [("user", "hello")])
        self.window._open_tools("mcp")
        self.assertIn("由 Sec Client 管理", self.window._tool_page._explanation.text())
        self.assertFalse(self.window._tool_page._enabled.isEnabled())

    def test_agent_category_is_single_selection_and_reload_remains_available_while_busy(self) -> None:
        page = self.window._tool_page
        self.window._runtime_ready("workspace")
        self.window._set_resources(ToolInventory((
            ToolEntry("agent", "default", "General assistant", "Current Agent", True),
            ToolEntry(
                "agent", "writer", "Writing specialist · 已排队，将在下一次 Run 生效",
                "Next Run", False,
            ),
            ToolEntry("mcp", "docs", "Docs · connected", "Service status: connected", True),
        ), busy=True))
        self.window._open_tools("agent")
        self.assertEqual(page._list.selectionMode(), page._list.SelectionMode.SingleSelection)
        self.assertEqual(page._list.currentItem().data(Qt.ItemDataRole.UserRole).name, "default")
        self.assertFalse(page._enabled.isVisible())
        requests = []
        reloads = []
        page.agent_requested.disconnect()
        page.reload_requested.disconnect()
        page.agent_requested.connect(requests.append)
        page.reload_requested.connect(lambda: reloads.append(True))
        page._list.setCurrentRow(1)
        self.assertEqual(requests, ["writer"])
        page.set_inventory(ToolInventory((
            ToolEntry("mcp", "docs", "Docs · connected", "Service status: connected", True),
        ), busy=True))
        self.window._open_tools("mcp")
        self.assertTrue(page._enabled.isEnabled())
        self.assertTrue(page._reload.isEnabled())
        page._reload.click()
        self.assertEqual(reloads, [True])

    def test_worker_plugin_mode_supports_hot_configuration_without_changing_global_permissions(self) -> None:
        page = self.window._tool_page
        page.activation_requested.disconnect()
        requests = []
        page.activation_requested.connect(lambda *args: requests.append(args))
        self.window._runtime_ready("workspace")
        self.window._set_resources(ToolInventory((
            ToolEntry("extension", "hello", "Tools", "Trusted plugin", False, "worker"),
        ), busy=True))
        self.window._open_tools("extension")
        self.assertTrue(page._execution_mode.isVisible())
        self.assertEqual(page._execution_mode.currentData(), "worker")
        self.assertTrue(page._enabled.isEnabled())
        self.assertFalse(page._mode.isEnabled())
        page._enabled.click()
        self.assertEqual(requests, [("worker", "hello", True)])
        self.window._set_resources(ToolInventory((
            ToolEntry("extension", "hello", "Tools", "Trusted plugin", True, "worker"),
        ), busy=True))
        self.assertFalse(page._execution_mode.isEnabled())
        self.assertTrue(page._enabled.isEnabled())

    def test_activation_and_permissions_wait_for_runtime_confirmation(self) -> None:
        page = self.window._tool_page
        self.window._runtime_ready("workspace")
        self.window._set_resources(ToolInventory((
            ToolEntry("skill", "review", "Review code", "Review content", True),
        )))
        self.window._open_tools("skill")
        activations = []
        modes = []
        page.activation_requested.disconnect()
        page.permission_requested.disconnect()
        page.activation_requested.connect(lambda *args: activations.append(args))
        page.permission_requested.connect(modes.append)
        page._enabled.click()
        self.assertEqual(activations, [("skill", "review", False)])
        self.assertTrue(page._enabled.isChecked())
        self.assertFalse(page._enabled.isEnabled())
        self.window._set_resources(ToolInventory((
            ToolEntry("skill", "review", "Review code", "Review content", False),
        )))
        self.assertFalse(page._enabled.isChecked())
        self.assertTrue(page._enabled.isEnabled())
        page._change_mode(page._mode.findData("plan"))
        self.assertEqual(modes, ["plan"])
        self.assertEqual(page._mode.currentData(), "normal")
        self.window._update_status("正在运行")
        self.assertFalse(page._enabled.isEnabled())
        self.assertFalse(page._mode.isEnabled())

    def test_session_row_date_format_and_single_line_full_id(self) -> None:
        from datetime import datetime
        from sec.apps.desktop.session_list import SessionItemDelegate

        timestamp = datetime(2026, 10, 18, 12, 26).timestamp()
        session_id = "04a410b31de747c7a68342906dc16baa"
        self.window._set_session_id(session_id)
        self.window._set_sessions(((session_id, "Recent conversation", timestamp),))
        item = self.window._sessions.item(0)
        self.assertEqual(item.text(), "Recent conversation")
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 2), "26-10-18 12:26")
        self.assertIsInstance(self.window._sessions.itemDelegate(), SessionItemDelegate)
        self.assertEqual(self.window._session_caption.text(), f"SessionID: {session_id}")
        self.assertFalse(self.window._session_caption.wordWrap())
        self.window.resize(900, 600)
        self.app.processEvents()
        caption = self.window._session_caption
        self.assertGreaterEqual(caption.width(), caption.fontMetrics().horizontalAdvance(caption.text()))

    def test_control_shortcuts_preserve_quotes_and_newlines_and_idle_draft(self) -> None:
        editor = self.window._input
        for modifiers, command in (
            (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, "steer"),
            (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier, "followup"),
        ):
            self.window._update_status("正在运行")
            text = 'Use "quotes" and apostrophe\'s\nsecond line'
            editor.setPlainText(text)
            with patch.object(self.window._runtime, "submit_control") as submit:
                QTest.keyClick(editor, Qt.Key.Key_Return, modifiers)
                submit.assert_called_once_with(command, text)
                self.assertEqual(editor.toPlainText(), "")
        self.window._update_status("已完成")
        editor.setPlainText("keep idle draft")
        with patch.object(self.window._runtime, "submit_control") as submit:
            QTest.keyClick(
                editor, Qt.Key.Key_Return,
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
            )
            submit.assert_not_called()
        self.assertEqual(editor.toPlainText(), "keep idle draft")

    def test_send_shortcut_works_with_completion_popup_open(self) -> None:
        editor = self.window._input
        editor.set_commands((("/help", "Show commands"),))
        editor.setPlainText("/he")
        editor.moveCursor(editor.textCursor().MoveOperation.End)
        editor._update_completion()
        with patch.object(self.window._runtime, "submit_message") as submit:
            QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            submit.assert_called_once_with("/he", "replace")

    def test_split_send_modes_and_permission_menu_dispatch(self) -> None:
        self.window._runtime_ready("workspace")
        self.window._input.setPlainText("new task")
        with patch.object(self.window._runtime, "submit_message") as submit:
            self.window._send.click()
        submit.assert_called_once_with("new task", "replace")
        self.window._update_status("正在运行")
        self.assertFalse(self.window._mode_button.isEnabled())
        for mode, label in (("steer", "引导"), ("followup", "追加")):
            self.window._input.setPlainText("control message")
            self.window._send_actions[mode].trigger()
            self.assertEqual(self.window._send.text(), label)
            with patch.object(self.window._runtime, "submit_control") as control:
                self.window._send.click()
            control.assert_called_once_with(mode, "control message")
        self.window._update_status("已完成")
        self.window._input.setPlainText("keep draft")
        with patch.object(self.window._runtime, "submit_control") as control:
            self.window._send.click()
        control.assert_not_called()
        self.assertEqual(self.window._input.toPlainText(), "keep draft")
        with patch.object(self.window._runtime, "configure_permission") as change:
            self.window._mode_actions["plan"].trigger()
        change.assert_called_once_with("plan")
        self.window._set_resources(ToolInventory(permission_mode="plan"))
        self.assertEqual(self.window._mode_button.text(), "mode · 规划")
        self.assertEqual(self.window._tool_page._mode.currentData(), "plan")

    def test_merged_button_stops_only_when_running_without_input(self) -> None:
        self.window._runtime_ready("workspace")
        self.window._update_status("正在运行")
        self.assertEqual(self.window._send.text(), "停止")
        with patch.object(self.window._runtime, "cancel_run") as cancel:
            self.window._send.click()
        cancel.assert_called_once()
        self.window._input.setPlainText("new request")
        self.assertEqual(self.window._send.text(), "发送")
        self.window._set_send_mode("steer")
        self.assertEqual(self.window._send.text(), "引导")
        self.window._input.clear()
        self.assertEqual(self.window._send.text(), "停止")
        self.window._run_finished()
        self.assertEqual(self.window._send.text(), "引导")

    def test_only_primary_stop_button_changes_background(self) -> None:
        self.assertIn("border: 1px solid #252b35; padding: 9px 16px", self.window.styleSheet())
        self.assertIn("border: 1px solid #252b35", self.window._send_options.styleSheet())
        self.assertIsNotNone(self.window._mode_button.menu())
        self.assertIn("QPushButton::menu-indicator { image: none; width: 0; }",
                      self.window._mode_button.styleSheet())
        self.window._runtime_ready("workspace")
        self.window._update_status("正在运行")
        self.assertTrue(self.window._send.property("stopping"))
        self.assertEqual(self.window._send.palette().color(QPalette.ColorRole.Button).name(), "#ffffff")
        self.assertEqual(self.window._send.palette().color(QPalette.ColorRole.ButtonText).name(), "#414854")
        self.assertFalse(self.window._send_options.property("stopping"))
        self.assertEqual(self.window._send_options.palette().color(QPalette.ColorRole.Button).name(), "#252b35")
        self.assertEqual(self.window._send_options.palette().color(QPalette.ColorRole.ButtonText).name(), "#ffffff")
        self.window._input.setPlainText("new request")
        for button in (self.window._send, self.window._send_options):
            self.assertFalse(button.property("stopping"))
            self.assertEqual(button.palette().color(QPalette.ColorRole.Button).name(), "#252b35")
        self.window._input.clear()
        self.assertTrue(self.window._send.property("stopping"))
        self.window._run_finished()
        self.assertFalse(self.window._send.property("stopping"))

    def test_send_segments_remain_aligned_when_stopping(self) -> None:
        self.window._runtime_ready("workspace")
        for active, draft in ((False, ""), (True, ""), (True, "request"), (False, "")):
            self.window._update_status("正在运行" if active else "就绪")
            self.window._input.setPlainText(draft)
            self.app.processEvents()
            left = self.window._send.geometry()
            right = self.window._send_options.geometry()
            self.assertEqual(left.top(), right.top())
            self.assertEqual(left.height(), right.height())
            self.assertEqual(left.height(), 40)
            for button in (self.window._send, self.window._send_options):
                self.assertTrue(button.testAttribute(Qt.WidgetAttribute.WA_LayoutUsesWidgetRect))

    def test_command_insert_uses_icon_and_chinese_tooltip(self) -> None:
        from PySide6.QtWidgets import QPushButton
        self.window._set_command_completions((("/new", "Start a Session"),))
        page = self.window._commands_page
        insert = next(
            button for button in page.findChildren(QPushButton)
            if button.toolTip() == "填入输入框"
        )
        self.assertEqual(insert.text(), "")
        self.assertFalse(insert.icon().pixmap(16, 16).isNull())
        self.assertEqual(insert.accessibleName(), "填入输入框")
        with patch.object(self.window._runtime, "submit_input") as dispatch:
            insert.click()
        dispatch.assert_not_called()
        self.assertEqual(self.window._input.toPlainText(), "/new ")

    def test_shortcuts_select_corresponding_mode_and_menu_checkmark(self) -> None:
        self.window._runtime_ready("workspace")
        self.window._update_status("正在运行")
        for mode, modifiers in (
            ("steer", Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier),
            ("followup", Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier),
            ("replace", Qt.KeyboardModifier.ControlModifier),
        ):
            self.window._input.setPlainText("shortcut request")
            with (
                patch.object(self.window._runtime, "submit_control") as control,
                patch.object(self.window._runtime, "submit_message") as send,
            ):
                QTest.keyClick(self.window._input, Qt.Key.Key_Return, modifiers)
            self.assertEqual(self.window._send_mode, mode)
            self.assertTrue(self.window._send_actions[mode].isChecked())
            self.assertEqual(self.window._send.text(), "停止")
            if mode == "replace":
                send.assert_called_once_with("shortcut request", "replace")
            else:
                control.assert_called_once_with(mode, "shortcut request")

    def test_session_menu_targets_clicked_session_and_delete_requires_confirmation(self) -> None:
        self.window._set_session_id("current")
        self.window._set_sessions((("other", "Other", None),))
        menu = self.window._build_session_menu(self.window._sessions.item(0))
        actions = {action.text(): action for action in menu.actions()}
        self.assertEqual(len(actions), 8)
        self.assertTrue(all(action.icon().isNull() for action in menu.actions()))
        self.assertIn("background: #edf2fa", menu.styleSheet())
        self.assertIn("border-radius: 8px", menu.styleSheet())
        for label, command in (
            ("打开会话", "open"), ("分支并打开", "fork"), ("完整克隆并打开", "clone"),
        ):
            with patch.object(self.window._runtime, "session_action") as dispatch:
                actions[label].trigger()
                dispatch.assert_called_once_with(command, "other", None)
        with (
            patch("sec.apps.desktop.window.confirm_delete", return_value=False),
            patch.object(self.window._runtime, "session_action") as dispatch,
        ):
            actions["删除会话…"].trigger()
            dispatch.assert_not_called()
        with (
            patch("sec.apps.desktop.window.confirm_delete", return_value=True),
            patch.object(self.window._runtime, "session_action") as dispatch,
        ):
            actions["删除会话…"].trigger()
            dispatch.assert_called_once_with("delete", "other", None)
        actions["复制 Session ID"].trigger()
        self.assertEqual(QApplication.clipboard().text(), "other")

    def test_header_name_click_renames_current_session_and_survives_history_refresh(self) -> None:
        self.assertFalse(self.window._title.isEnabled())
        self.window._set_session_id("current")
        self.window._set_sessions((("current", "Existing name", None), ("other", "Other", None)))
        self.window._set_session_history((("user", "hello"),))
        self.assertEqual(self.window._title.text(), "Existing name")
        with (
            patch("sec.apps.desktop.window.rename_session", return_value=" Updated ") as dialog,
            patch.object(self.window._runtime, "rename_session") as rename,
        ):
            QTest.mouseClick(self.window._title, Qt.MouseButton.LeftButton)
        dialog.assert_called_once_with(self.window, "Existing name")
        rename.assert_called_once_with("current", "Updated")
        self.window._set_sessions((("current", "Updated", None),))
        self.window._set_session_history((("user", "hello"), ("assistant", "reply")))
        self.assertEqual(self.window._title.text(), "Updated")
        with (
            patch("sec.apps.desktop.window.rename_session", return_value=None),
            patch.object(self.window._runtime, "rename_session") as rename,
        ):
            self.window._title.click()
        rename.assert_not_called()
        self.window._set_session_id("new-session")
        self.window._set_session_history(())
        self.assertEqual(self.window._title.text(), "新对话")

    def test_completed_activity_collapses_and_preserves_manual_expansion_on_refresh(self) -> None:
        history = (
            ("user", "short request"),
            ("activity", "工具完成 · read_file\nFull result text"),
            ("assistant", "Final answer"),
        )
        self.window._set_session_id("current")
        self.window._set_session_history(history)
        bubble = self.window._transcript.bubbles[1]
        self.assertTrue(bubble.body.isHidden())
        bubble.toggle.click()
        self.assertFalse(bubble.body.isHidden())
        self.assertIn("Full result text", bubble.body.toPlainText())
        self.window._set_session_history(history)
        bubble = self.window._transcript.bubbles[1]
        self.assertTrue(bubble.toggle.isChecked())
        self.assertFalse(bubble.body.isHidden())
        self.assertFalse(self.window._transcript.bubbles[0].body.isHidden())
        self.assertFalse(self.window._transcript.bubbles[2].body.isHidden())
        self.window._set_session_id("other")
        self.window._set_session_history(history)
        self.assertTrue(self.window._transcript.bubbles[1].body.isHidden())

    def test_turn_groups_intermediate_messages_but_keeps_final_markdown_outside(self) -> None:
        self.window._set_session_timing((1000, 1010, 1015, 1020, 1025, 1167, 2000, 2010))
        self.window._set_session_history((
            ("user", "Inspect code"),
            ("assistant", "I will inspect **two files**."),
            ("activity", "调用工具 · read_file\n{}"),
            ("activity", "工具完成 · read_file\nFile contents"),
            ("activity", "审批允许 · read_file"),
            ("assistant", "## Review\n\n**All done**"),
            ("user", "Next question"),
            ("assistant", "Second answer"),
        ))
        chat = self.window._transcript
        self.assertEqual(len(chat.process_groups), 1)
        group = chat.process_groups[0]
        self.assertEqual(group.count, 4)
        self.assertIn("耗时 2分47秒", group.toggle.text())
        self.assertEqual(chat.bubbles[1].timestamp, 1010)
        self.assertTrue(group.details.isHidden())
        self.assertEqual(chat.bubbles[1].role, "process")
        self.assertEqual(chat.bubbles[5].role, "assistant")
        self.assertIn("All done", chat.bubbles[5].body.toPlainText())
        self.assertNotIn("**All done**", chat.bubbles[5].body.toPlainText())
        self.assertLess(
            chat.bubbles[1].body.font().pointSizeF(),
            chat.bubbles[5].body.font().pointSizeF(),
        )


if __name__ == "__main__":
    unittest.main()
