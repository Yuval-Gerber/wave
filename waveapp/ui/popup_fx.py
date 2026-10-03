"""Modal open animation (Phase 8.14, §5): every modal is a centered
card over a dimmed backdrop with a subtle scale animation. One helper, used
by all overlay popups, so they feel identical.

Implementation notes, learned the hard way (suite segfaults): ANY Python
callback wired to the animation (finished/valueChanged, opacity effects,
deferred timers) can fire from inside a destructor cascade — the animation
is stopped while its owner widget is half-destroyed, sip still reports the
widget alive, and touching it crashes. So this helper wires NO callbacks:
one pure C++ geometry animation, parented to the card, nothing else.
"""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRect
from PyQt6.QtWidgets import QWidget

_UNBOUNDED = 16_777_215  # Qt's QWIDGETSIZE_MAX


def pop_in(overlay: QWidget, card: QWidget) -> None:
    """Scale the card up from 94% to its final rect (190ms, OutCubic).

    Call at the end of the popup's __init__ (card already sized/centered).
    Fixed-size cards are unbounded FOR GOOD — setGeometry is clamped by
    min/max (which would kill the scale), and restoring them afterward
    would need a finished-callback (see module docstring). Popup cards are
    positioned manually, so nothing else ever resizes them.
    """
    end = card.geometry()
    inset_x = max(1, int(end.width() * 0.03))
    inset_y = max(1, int(end.height() * 0.03))
    start = end.adjusted(inset_x, inset_y, -inset_x, -inset_y)
    card.setMinimumSize(0, 0)
    card.setMaximumSize(_UNBOUNDED, _UNBOUNDED)
    scale = QPropertyAnimation(card, b"geometry", card)
    scale.setDuration(190)
    scale.setEasingCurve(QEasingCurve.Type.OutCubic)
    scale.setStartValue(QRect(start))
    scale.setEndValue(QRect(end))
    scale.start()
    overlay._pop_fx = scale  # keep alive
