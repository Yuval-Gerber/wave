"""UI (SPEC.md §5, Phases 1 & 8): PyQt6 shell, animated wave logo top bar,
hover sidebar, and the six tabs. Observes engine state via Qt signals; commands
go through the CommandBus only."""

# GLOBAL InfiniteLine teardown guard (2026-09-15): finplot's own crosshair
# lines (not just ours) crash pyqtgraph's _computeBoundingRect when a paint
# lands after the ViewBox is gone — the rehearsal-suite segfault. Patch the
# class once, at UI import: with no viewbox, return the last known rect.
try:  # pragma: no cover — trivial guard, exercised by the whole UI suite
    import pyqtgraph as _pg

    _orig_cbr = _pg.InfiniteLine._computeBoundingRect

    def _safe_cbr(self):
        # try/except, not check-then-call: the ViewBox weakref can die
        # BETWEEN a getViewBox() pre-check and the original's own call
        # (build25 proved it — both pre-checks passed, then None inside).
        try:
            return _orig_cbr(self)
        except AttributeError:
            br = getattr(self, "_boundingRect", None)
            return br if br is not None else _pg.QtCore.QRectF()

    if getattr(_pg.InfiniteLine._computeBoundingRect, "__name__", "") != "_safe_cbr":
        _pg.InfiniteLine._computeBoundingRect = _safe_cbr
except Exception:  # noqa: S110 — a guard must never break imports
    pass
