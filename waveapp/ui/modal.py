"""WaveModal (8.1): the one modal template for the whole app (SPEC.md §5 —
"all modals are centered popup cards with dimmed backdrop and subtle
scale/fade animation").

An overlay fills the parent window (dimmed, fades in), with a centered card
that scales 0.96→1.0 while fading. The card gets the native glass POPOVER
material via pyqt-liquidglass when available (within-window blending), with a
translucent solid fallback. Esc or a backdrop click dismisses (configurable).
"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    Qt,
    pyqtSignal,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from waveapp.ui import theme

logger = logging.getLogger("wave.ui.modal")


class WaveModal(QWidget):
    """Overlay + centered glass card. Use `present()` / `dismiss()`."""

    dismissed = pyqtSignal()

    def __init__(
        self,
        parent: QWidget,
        title: str,
        content: QWidget,
        close_on_backdrop: bool = True,
        show_close_button: bool = True,
    ) -> None:
        super().__init__(parent)
        self._close_on_backdrop = close_on_backdrop
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("background: rgba(0, 0, 0, 0.28);")  # dimmed backdrop
        self.setGeometry(parent.rect())

        self.card = QFrame(self)
        self.card.setObjectName("waveModalCard")
        self.card.setStyleSheet(
            f"#waveModalCard {{ background: rgba(250, 250, 252, 0.92);"
            f" border: 1px solid {theme.BORDER};"
            f" border-radius: {theme.RADIUS_MODAL}px; }}"
        )

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(theme.SPACE_XL, theme.SPACE_L, theme.SPACE_XL, theme.SPACE_XL)
        layout.setSpacing(theme.SPACE_M)

        header = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setProperty("cardTitle", True)
        header.addWidget(title_label)
        header.addStretch(1)
        if show_close_button:
            close = QPushButton("✕")
            close.setFixedSize(26, 26)
            close.setStyleSheet(
                f"QPushButton {{ border: none; border-radius: 13px; background:"
                f" rgba(0,0,0,0.06); color: {theme.TEXT_MUTED}; font-size: 12px; }}"
                f"QPushButton:hover {{ background: rgba(0,0,0,0.12); }}"
            )
            close.clicked.connect(self.dismiss)
            header.addWidget(close)
        layout.addLayout(header)
        layout.addWidget(content)

        # animations: backdrop fade + card scale/fade
        self._backdrop_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._backdrop_effect)
        self._fade = QPropertyAnimation(self._backdrop_effect, b"opacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._scale = QPropertyAnimation(self.card, b"geometry", self)
        self._scale.setDuration(200)
        self._scale.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._group = QParallelAnimationGroup(self)
        self._group.addAnimation(self._fade)
        self._group.addAnimation(self._scale)

    # -- lifecycle -----------------------------------------------------------

    def _center_geometry(self, scale: float = 1.0):
        hint = self.card.sizeHint()
        width = int(hint.width() * scale)
        height = int(hint.height() * scale)
        return (
            (self.width() - width) // 2,
            (self.height() - height) // 2,
            width,
            height,
        )

    def present(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self._apply_glass()
        from PyQt6.QtCore import QRect

        start = QRect(*self._center_geometry(0.96))
        end = QRect(*self._center_geometry(1.0))
        self.card.setGeometry(start)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._scale.setStartValue(start)
        self._scale.setEndValue(end)
        self._group.start()

    def dismiss(self) -> None:
        self.hide()
        self.dismissed.emit()
        self.deleteLater()

    def _apply_glass(self) -> None:
        """Native POPOVER glass on the card (within-window); solid fallback.
        Never under the offscreen platform: the native view calls SEGFAULT
        without a real window (found by the test suite, 2026-08-14)."""
        import os

        if sys.platform != "darwin" or os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        try:
            import pyqt_liquidglass as glass

            effect = glass.apply_glass_to_widget(
                self.card,
                options=glass.GlassOptions(
                    corner_radius=float(theme.RADIUS_MODAL),
                    material=glass.GlassMaterial.POPOVER,
                    blending_mode=glass.BlendingMode.WITHIN_WINDOW,
                ),
            )
            if effect is not None:
                self.card.setStyleSheet(
                    f"#waveModalCard {{ background: rgba(250, 250, 252, 0.55);"
                    f" border: 1px solid {theme.BORDER};"
                    f" border-radius: {theme.RADIUS_MODAL}px; }}"
                )
        except Exception:
            logger.debug("modal glass unavailable — translucent fallback")

    # -- input ---------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._close_on_backdrop and not self.card.geometry().contains(
            event.position().toPoint()
        ):
            self.dismiss()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self._close_on_backdrop:
            self.dismiss()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtCore import QRect

        if self.card.isVisible():
            self.card.setGeometry(QRect(*self._center_geometry(1.0)))
        super().resizeEvent(event)
