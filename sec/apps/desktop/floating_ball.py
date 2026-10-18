"""Compact launcher for the existing chat window."""

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QWidget

from sec.apps.desktop.appearance import application_icon


class FloatingBall(QWidget):
    restore_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Sec Desktop · 悬浮球")
        self.setWindowIcon(application_icon())
        self.setFixedSize(72, 72)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Sec 悬浮球，点击返回大屏专注")
        self._pixmap = application_icon().pixmap(144, 144)
        self._press_position: QPoint | None = None
        self._start_position = QPoint()
        self._dragging = False

    def move_on_screen(self, position: QPoint) -> None:
        screen = QApplication.screenAt(position) or self.screen()
        area = screen.availableGeometry()
        self.move(
            max(area.left(), min(position.x(), area.right() - self.width() + 1)),
            max(area.top(), min(position.y(), area.bottom() - self.height() + 1)),
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        clip = QPainterPath()
        clip.addEllipse(bounds)
        painter.setClipPath(clip)
        painter.drawPixmap(bounds.toRect(), self._pixmap)
        painter.setClipping(False)
        painter.setPen(QPen(QColor("#b9cce8"), 2))
        painter.drawEllipse(bounds)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.globalPosition().toPoint()
            self._start_position = self.pos()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_position is not None:
            delta = event.globalPosition().toPoint() - self._press_position
            if delta.manhattanLength() >= QApplication.startDragDistance():
                self._dragging = True
            if self._dragging:
                self.move_on_screen(self._start_position + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._press_position is not None
        ):
            self._press_position = None
            if not self._dragging:
                self.restore_requested.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        menu = QMenu(self)
        restore = menu.addAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton),
            "返回大屏专注",
        )
        restore.triggered.connect(self.restore_requested.emit)
        menu.addSeparator()
        quit_action = menu.addAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton),
            "退出 Sec",
        )
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.exec(event.globalPos())
