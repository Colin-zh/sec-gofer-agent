"""Transient, non-blocking toast notifications for Sec Desktop.

Toasts are lightweight confirmations that auto-dismiss and stack in the
bottom-right corner of the window. They are intentionally transparent to the
mouse so they never steal clicks from the conversation underneath.
"""

from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

# kind -> (accent dot, background, border)
_KINDS = {
    "info": ("#3b82f6", "#eef4ff", "#dbe7fe"),
    "success": ("#22a06b", "#eaf7f0", "#d3eee0"),
    "error": ("#dc3545", "#fdecec", "#f7d4d8"),
    "warning": ("#f59e0b", "#fef6e7", "#f7e6c4"),
    "follow_up": ("#6b7280", "#f1f3f5", "#e3e7ec"),
}

_FALLBACK = ("#6b7280", "#f1f3f5", "#e3e7ec")

# Longer linger for messages users may need to read carefully.
_DURATIONS = {"error": 6500, "warning": 5000, "follow_up": 3000}


class Toast(QFrame):
    def __init__(self, message: str, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        accent, background, border = _KINDS.get(kind, _FALLBACK)
        self.setMaximumWidth(420)
        self.setStyleSheet(
            f"QFrame#toast {{ background: {background}; border: 1px solid {border};"
            f" border-radius: 10px; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(9)

        dot = QLabel("●")
        dot.setFixedWidth(12)
        dot.setStyleSheet(
            f"color: {accent}; font-size: 10px; background: transparent; border: 0;"
        )

        label = QLabel(message)
        label.setWordWrap(True)
        label.setStyleSheet(
            "color: #202124; background: transparent; border: 0; font-size: 13px;"
        )

        layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(label, 1)


class ToastManager(QWidget):
    """Overlay that stacks toasts bottom-right and auto-dismisses them."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(20, 20, 20, 20)
        self._layout.setSpacing(8)
        self._layout.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight
        )
        self._active: list[tuple[Toast, QTimer]] = []
        self.hide()

    def show_message(self, message: str, kind: str = "info") -> None:
        message = (message or "").strip()
        if not message:
            return
        toast = Toast(message, kind, self)
        self._layout.addWidget(toast)

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._dismiss(toast, timer))
        timer.start(_DURATIONS.get(kind, 3200))
        self._active.append((toast, timer))

        self.show()
        self.raise_()
        self._fade(toast, 0.0, 1.0, 160)

    def _fade(self, toast: Toast, start: float, end: float, duration: int) -> None:
        effect = toast.graphicsEffect()
        if effect is None:
            effect = QGraphicsOpacityEffect(toast)
            toast.setGraphicsEffect(effect)
        effect.setOpacity(start)
        animation = QPropertyAnimation(effect, b"opacity", toast)
        animation.setDuration(duration)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)

    def _dismiss(self, toast: Toast, timer: QTimer) -> None:
        if (toast, timer) not in self._active:
            return
        self._active.remove((toast, timer))
        timer.stop()
        self._layout.removeWidget(toast)
        effect = toast.graphicsEffect()
        if effect is None:
            toast.deleteLater()
        else:
            animation = QPropertyAnimation(effect, b"opacity", toast)
            animation.setDuration(180)
            animation.setStartValue(effect.opacity())
            animation.setEndValue(0.0)
            animation.finished.connect(toast.deleteLater)
            animation.start()
        if not self._active:
            self.hide()
