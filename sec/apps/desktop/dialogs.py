"""Light-themed, cancellable desktop dialogs.

Session helpers return user input only after explicit acceptance; they do not
rename, delete, or export session data themselves.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QLabel, QLineEdit, QVBoxLayout,
    QWidget,
)


_STYLE = """
QDialog, QWidget { background: #f7f8fa; color: #202124; }
QLabel { background: transparent; color: #202124; }
QLineEdit, QComboBox, QAbstractItemView, QTextEdit, QPlainTextEdit {
    background: #fff; color: #202124; border: 1px solid #dfe3ea;
    border-radius: 8px; padding: 7px;
    selection-background-color: #dfe7f2; selection-color: #202124;
}
QLineEdit:focus, QComboBox:focus { border-color: #a8b8cf; }
QAbstractItemView::item:selected { background: #dfe7f2; color: #202124; }
QPushButton {
    background: #fff; color: #202124; border: 1px solid #dfe3ea;
    border-radius: 8px; padding: 7px 16px; min-height: 20px;
}
QPushButton:hover, QPushButton:focus, QPushButton:default {
    background: #edf2fa; border-color: #a8b8cf;
}
QPushButton:pressed { background: #dfe7f2; }
QPushButton:disabled { color: #9299a3; background: #f0f2f5; }
QPushButton#deleteSession { color: #b42318; border-color: #e4b7b3; }
"""


def _dialog(parent: QWidget | None, title: str, message: str) -> QDialog:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setStyleSheet(_STYLE)
    dialog.setMinimumWidth(360)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(16)
    label = QLabel(message)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    layout.addWidget(label)
    return dialog


def _buttons(
    dialog: QDialog, buttons: QDialogButtonBox.StandardButton,
) -> QDialogButtonBox:
    box = QDialogButtonBox(buttons, parent=dialog)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    dialog.layout().addWidget(box)
    return box


def rename_session(parent: QWidget | None, current_name: str) -> str | None:
    """Return the accepted session name, or None on cancellation."""
    dialog = _dialog(parent, "重命名会话", "会话名称")
    field = QLineEdit(current_name, dialog)
    field.setAccessibleName("会话名称")
    dialog.layout().addWidget(field)
    box = _buttons(
        dialog,
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
    )
    box.button(QDialogButtonBox.StandardButton.Save).setText("保存")
    box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    field.selectAll()
    field.setFocus()
    try:
        return field.text() if dialog.exec() == QDialog.DialogCode.Accepted else None
    finally:
        dialog.deleteLater()


def confirm_delete(parent: QWidget | None, session_id: str) -> bool:
    """Confirm irreversible deletion, defaulting to cancellation."""
    dialog = _dialog(
        parent, "删除会话",
        f"确定要删除会话“{session_id}”吗？\n此操作无法撤销。",
    )
    box = _buttons(
        dialog,
        QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel,
    )
    delete = box.button(QDialogButtonBox.StandardButton.Yes)
    delete.setText("删除会话")
    delete.setObjectName("deleteSession")
    delete.setAutoDefault(False)
    delete.setDefault(False)
    cancel = box.button(QDialogButtonBox.StandardButton.Cancel)
    cancel.setText("取消")
    cancel.setDefault(True)
    cancel.setFocus()
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()


def export_session(parent: QWidget | None, session_id: str) -> str | None:
    """Choose a JSON export destination without writing session data."""
    dialog = QFileDialog(parent)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, False)
    dialog.setWindowTitle("导出会话")
    dialog.setStyleSheet(_STYLE)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    dialog.setNameFilter("JSON (*.json)")
    dialog.setDefaultSuffix("json")
    # Session identifiers are labels, not directories supplied to the picker.
    filename = session_id.replace("/", "_").replace("\\", "_")
    dialog.selectFile(f"{filename}.json")
    try:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            files = dialog.selectedFiles()
            return files[0] if files else None
        return None
    finally:
        dialog.deleteLater()


def show_error(parent: QWidget | None, message: str) -> None:
    """Display an error as literal text in a dismissible desktop dialog."""
    dialog = _dialog(parent, "错误", message)
    box = _buttons(dialog, QDialogButtonBox.StandardButton.Ok)
    box.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()
