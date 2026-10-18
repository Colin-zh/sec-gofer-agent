"""Independent Qt coverage for the desktop dialog helpers."""

from pathlib import Path
import unittest
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QLabel, QLineEdit,
    QWidget,
)

from sec.apps.desktop import dialogs


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        self.parent = QWidget()
        self.addCleanup(self.parent.deleteLater)

    def inspect_rendering(self, dialog: QDialog) -> None:
        self.assertIs(dialog.parentWidget(), self.parent)
        self.assertIn("#f7f8fa", dialog.styleSheet())
        self.assertIn("#fff", dialog.styleSheet())
        self.assertIn("#202124", dialog.styleSheet())
        self.assertIn("border-radius: 8px", dialog.styleSheet())
        self.assertIn("#edf2fa", dialog.styleSheet())
        dialog.show()
        self.app.processEvents()
        self.assertFalse(dialog.grab().isNull())

    def test_rename_accepts_edited_name_and_selects_current_name(self) -> None:
        def execute(dialog: QDialog) -> int:
            self.inspect_rendering(dialog)
            field = dialog.findChild(QLineEdit)
            self.assertEqual(field.text(), "Original")
            self.assertEqual(field.selectedText(), "Original")
            field.setText("New name")
            dialog.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Save,
            ).click()
            return dialog.result()

        with patch.object(QDialog, "exec", execute):
            self.assertEqual(dialogs.rename_session(self.parent, "Original"), "New name")

    def test_rename_cancel_does_not_return_edited_text(self) -> None:
        def execute(dialog: QDialog) -> int:
            dialog.findChild(QLineEdit).setText("Discard me")
            dialog.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Cancel,
            ).click()
            return dialog.result()

        with patch.object(QDialog, "exec", execute):
            self.assertIsNone(dialogs.rename_session(self.parent, "Original"))

    def test_delete_requires_explicit_destructive_acceptance(self) -> None:
        def execute(dialog: QDialog) -> int:
            self.inspect_rendering(dialog)
            label = dialog.findChild(QLabel)
            self.assertIn("<session>", label.text())
            self.assertIn("无法撤销", label.text())
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
            box = dialog.findChild(QDialogButtonBox)
            cancel = box.button(QDialogButtonBox.StandardButton.Cancel)
            delete = box.button(QDialogButtonBox.StandardButton.Yes)
            self.assertTrue(cancel.isDefault())
            self.assertFalse(delete.isDefault())
            self.assertFalse(delete.autoDefault())
            self.assertEqual(delete.text(), "删除会话")
            delete.click()
            return dialog.result()

        with patch.object(QDialog, "exec", execute):
            self.assertTrue(dialogs.confirm_delete(self.parent, "<session>"))

    def test_delete_enter_defaults_to_cancel(self) -> None:
        def cancel_default() -> None:
            dialog = self.app.activeModalWidget()
            QTest.keyClick(dialog, Qt.Key.Key_Return)

        QTimer.singleShot(0, cancel_default)
        self.assertFalse(dialogs.confirm_delete(self.parent, "session"))

    def test_escape_and_window_close_cancel_session_dialogs(self) -> None:
        for action in ("escape", "close"):
            for helper, value, expected in (
                (dialogs.rename_session, "Name", None),
                (dialogs.confirm_delete, "session", False),
                (dialogs.export_session, "session", None),
            ):
                with self.subTest(action=action, helper=helper.__name__):
                    def dismiss() -> None:
                        dialog = self.app.activeModalWidget()
                        if action == "escape":
                            QTest.keyClick(dialog, Qt.Key.Key_Escape)
                        else:
                            dialog.close()

                    QTimer.singleShot(0, dismiss)
                    self.assertIs(helper(self.parent, value), expected)

    def test_export_is_non_native_json_save_with_overwrite_confirmation(self) -> None:
        def execute(dialog: QFileDialog) -> int:
            self.inspect_rendering(dialog)
            self.assertTrue(dialog.testOption(QFileDialog.Option.DontUseNativeDialog))
            self.assertFalse(dialog.testOption(QFileDialog.Option.DontConfirmOverwrite))
            self.assertEqual(dialog.acceptMode(), QFileDialog.AcceptMode.AcceptSave)
            self.assertEqual(dialog.fileMode(), QFileDialog.FileMode.AnyFile)
            self.assertEqual(dialog.defaultSuffix(), "json")
            self.assertEqual(dialog.nameFilters(), ["JSON (*.json)"])
            self.assertEqual(Path(dialog.selectedFiles()[0]).name, "session.json")
            dialog.findChild(QLineEdit, "fileNameEdit").setText("chosen-export")
            dialog.accept()
            return dialog.result()

        with patch.object(QFileDialog, "exec", execute):
            result = dialogs.export_session(self.parent, "session")
        self.assertEqual(result, str(Path.cwd() / "chosen-export.json"))
        self.assertFalse(Path(result).exists())

    def test_export_rejected_or_empty_selection_returns_none(self) -> None:
        with patch.object(QFileDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            self.assertIsNone(dialogs.export_session(self.parent, "session"))
        with (
            patch.object(QFileDialog, "exec", return_value=QDialog.DialogCode.Accepted),
            patch.object(QFileDialog, "selectedFiles", return_value=[]),
        ):
            self.assertIsNone(dialogs.export_session(self.parent, "session"))

    def test_error_renders_literal_message_and_ok_dismisses(self) -> None:
        def execute(dialog: QDialog) -> int:
            self.inspect_rendering(dialog)
            label = dialog.findChild(QLabel)
            self.assertEqual(label.text(), "<b>Failure & details</b>")
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
            dialog.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Ok,
            ).click()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            return dialog.result()

        with patch.object(QDialog, "exec", execute):
            self.assertIsNone(dialogs.show_error(self.parent, "<b>Failure & details</b>"))

    def test_error_escape_and_close_are_safe(self) -> None:
        for action in ("escape", "close"):
            def dismiss() -> None:
                dialog = self.app.activeModalWidget()
                if action == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                else:
                    dialog.close()

            QTimer.singleShot(0, dismiss)
            self.assertIsNone(dialogs.show_error(self.parent, "Failure"))
