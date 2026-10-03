"""Touch ID hard gates (SPEC.md §4).

One helper used by every gated action: paper→live switch, risk edits,
key rotation, kill switch, weekly-halt re-arm, kill re-arm. No session
carry-over — every call re-authenticates.

On Macs without Touch ID the biometric prompt can't run; the action
proceeds (the login password already gated the session) — same behavior
the Risk unlock shipped with in 8.10.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PyQt6.QtCore import QObject, pyqtSignal

from waveapp.security import auth


class _MainThreadInvoker(QObject):
    """Cross-thread bridge: a queued signal emitted from the auth worker
    runs the callback on the Qt main thread. (QTimer.singleShot must NOT
    be used here — timers can't start from a non-Qt thread, so the
    callback would silently never fire.)"""

    fire = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.fire.connect(lambda callback: callback())


_invoker: _MainThreadInvoker | None = None


def require_gate(reason: str, on_success: Callable[[], None]) -> None:
    """Run Touch ID on a worker thread; call `on_success` on the Qt thread."""
    global _invoker
    if not auth.touch_id_available():
        on_success()
        return
    if _invoker is None:
        # require_gate is always called from the Qt thread (button clicks),
        # so the invoker — and its queued connection target — live there
        _invoker = _MainThreadInvoker()
    invoker = _invoker

    def worker() -> None:
        if auth.authenticate_touch_id(reason):
            invoker.fire.emit(on_success)

    threading.Thread(target=worker, daemon=True).start()
