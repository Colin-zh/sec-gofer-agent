import unittest

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from sec.apps.desktop.conversation import Conversation


class ConversationProcessTests(unittest.TestCase):
    def test_user_navigation_and_return_to_bottom_preserve_content(self) -> None:
        chat = Conversation()
        chat.resize(650, 360)
        chat.show()
        self.addCleanup(chat.close)
        for i in range(24):
            chat.add_message("user", f"Request {i}")
            chat.add_message("assistant", "Answer\n" * 8)
        self.settle()
        bar = chat.verticalScrollBar()
        self.assertEqual(len(chat._user_targets), 24)
        self.assertFalse(chat._bottom_button.isVisible())
        bubble, marker = chat._user_targets[4]
        self.assertIn("Request 4", marker.toolTip())
        marker.click()
        self.settle()
        self.assertLessEqual(abs(bubble.mapTo(chat.viewport(), QPoint()).y() - 12), 3)
        self.assertTrue(marker.isChecked())
        self.assertFalse(chat._navigation.viewport().autoFillBackground())
        self.assertIn("background: transparent", chat._navigation.widget().styleSheet()
                      or chat._navigation.styleSheet())
        self.assertIn("color: #000000", marker.styleSheet())
        self.assertTrue(chat._bottom_button.isVisible())
        previous = bar.value()
        chat.add_message("assistant", "Streaming output\n" * 12)
        self.settle()
        self.assertEqual(bar.value(), previous)
        chat._bottom_button.click()
        self.settle()
        self.assertEqual(bar.value(), bar.maximum())
        self.assertFalse(chat._bottom_button.isVisible())
        chat.add_message("assistant", "More output\n" * 12)
        self.settle()
        self.assertEqual(bar.value(), bar.maximum())
        self.assertEqual(len(chat.bubbles), 50)
        chat.clear()
        self.settle()
        self.assertEqual(chat._user_targets, [])
        self.assertTrue(chat._navigation.isHidden())
        self.assertTrue(chat._bottom_button.isHidden())

    def test_persisted_record_times_and_total_elapsed_are_displayed(self) -> None:
        from datetime import datetime
        chat = Conversation()
        self.addCleanup(chat.close)
        start = datetime(2026, 10, 18, 14, 20, 0).timestamp()
        chat.add_message("user", "Request", timestamp=start)
        step = chat.add_message("activity", "工具完成 · probe\nDetails", timestamp=start + 45)
        chat.add_message("assistant", "Answer", timestamp=start + 167)
        group = chat.process_groups[0]
        self.assertIn("耗时 2分47秒", group.toggle.text())
        self.assertIn("14:20:45", step.toggle.text())
        self.assertEqual(step.toggle.toolTip(), "2026-10-18 14:20:45")
        step.toggle.click()
        self.assertIn("14:20:45", step.toggle.text())
        self.assertIn("耗时 2分47秒", group.toggle.text())
        chat.clear()
        missing = chat.add_message("activity", "Legacy record")
        self.assertNotIn("耗时", chat.process_groups[0].toggle.text())
        self.assertEqual(missing.toggle.toolTip(), "")

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def settle(self) -> None:
        for _ in range(8):
            self.app.processEvents()

    def test_group_and_nested_toggle_keep_viewport_position_not_bottom(self) -> None:
        chat = Conversation()
        chat.resize(650, 360)
        chat.show()
        self.addCleanup(chat.close)
        for i in range(18):
            chat.add_message("user", f"Request {i}")
            chat.add_message("activity", f"工具完成 · probe {i}\n" + "details\n" * 15)
            chat.add_message("assistant", f"## Answer {i}\n\n**Result**\n\n" + "explanation\n" * 4)
        self.settle()
        group = chat.process_groups[4]
        bar = chat.verticalScrollBar()
        bar.setValue(group.mapTo(chat._content, QPoint()).y() - 60)
        self.settle()
        before = group.toggle.mapTo(chat.viewport(), QPoint()).y()
        group.toggle.click()
        self.settle()
        self.assertLessEqual(abs(group.toggle.mapTo(chat.viewport(), QPoint()).y() - before), 3)
        self.assertLess(bar.value(), bar.maximum() - 100)
        step = group.steps.itemAt(0).widget()
        before = step.toggle.mapTo(chat.viewport(), QPoint()).y()
        step.toggle.click()
        self.settle()
        self.assertLessEqual(abs(step.toggle.mapTo(chat.viewport(), QPoint()).y() - before), 3)
        self.assertLess(bar.value(), bar.maximum() - 100)
        step.toggle.click()
        group.toggle.click()
        self.settle()
        self.assertLess(bar.value(), bar.maximum() - 100)
        self.assertTrue(group.details.isHidden())
        scroll_before = bar.value()
        chat.begin_refresh()
        chat.clear()
        for i in range(18):
            chat.add_message("user", f"Request {i}")
            chat.add_message("activity", f"工具完成 · probe {i}\n" + "details\n" * 15)
            chat.add_message("assistant", f"## Answer {i}\n\n**Result**\n\n" + "explanation\n" * 4)
        chat.end_refresh()
        self.settle()
        self.assertLessEqual(abs(bar.value() - scroll_before), 3)

    def test_follow_tail_only_when_reading_latest_and_markdown_is_rendered(self) -> None:
        chat = Conversation()
        chat.resize(650, 300)
        chat.show()
        self.addCleanup(chat.close)
        for i in range(20):
            chat.add_message("assistant", f"## Title {i}\n\n**bold**\n\n" + "line\n" * 5)
        self.settle()
        bar = chat.verticalScrollBar()
        bar.setValue(100)
        chat.add_message("assistant", "New answer\n" * 20)
        self.settle()
        self.assertEqual(bar.value(), 100)
        bar.setValue(bar.maximum())
        bubble = chat.add_message("assistant", "## Heading\n\n**Bold**\n\n" + "last\n" * 20)
        self.settle()
        self.assertEqual(bar.value(), bar.maximum())
        self.assertNotIn("**Bold**", bubble.body.toPlainText())
        self.assertIn("Bold", bubble.body.toPlainText())
