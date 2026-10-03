"""Wave application entry (Phase 1).

Flow: logging setup → QApplication + theme → login (Touch ID/password) →
MainWindow under a qasync event loop (the asyncio loop later phases' engine
runs on). Login happens before the loop starts, so its exec() can block safely.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import os
import sys

import qasync
from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRect, QTimer
from PyQt6.QtWidgets import QApplication

from waveapp import __version__
from waveapp.broker.base import TradingMode
from waveapp.config import AppConfig, log_dir
from waveapp.engine.connection_monitor import ConnectionMonitor
from waveapp.persistence.db import Database
from waveapp.persistence.db_log import DBLogHandler
from waveapp.security.auth import PasswordAuth
from waveapp.ui.main_window import MainWindow
from waveapp.ui.theme import APP_QSS

logger = logging.getLogger("wave")

# -- fire-and-forget task plumbing (audit A5-6 class, 2026-09-22) -------------
# asyncio keeps only WEAK references to running tasks: a bare
# `asyncio.ensure_future(...)` whose returned task is dropped can be
# garbage-collected mid-flight (the 2026-08-19 segfault class) — a clicked
# Test/Save/command silently never finishing. The Qt signal connections hold
# the *closures*, never the tasks they create, so every fire-and-forget in
# this module routes through _spawn: strong ref until done, and any exception
# the task swallowed is logged loudly with its label. (The monitor has its
# own _spawn for the same reason — deliberately not shared.)
_UI_TASKS: set[asyncio.Task] = set()


def _spawn(coro, what: str) -> asyncio.Task | None:
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:  # no running loop (shutdown edge) — nothing to run
        coro.close()
        logger.warning("UI task '%s' dropped — no running event loop", what)
        return None
    _UI_TASKS.add(task)

    def _done(t: asyncio.Task, what: str = what) -> None:
        _UI_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("UI task '%s' failed: %r", what, t.exception())

    task.add_done_callback(_done)
    return task


def _monitor_task_died(task: asyncio.Task) -> None:
    """A5-4 handoff (audit 2026-09-22): monitor.run() is supervised per-cycle
    inside the monitor, so this callback should never fire except through
    quit cancellation — if it does (a raise inside _teardown after
    CancelledError, or the task finalized some other way), the dots freeze
    green with nothing polling behind them, so scream."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.critical("MONITOR LOOP DIED: %r", exc, exc_info=exc)
    else:
        logger.critical("MONITOR LOOP DIED: run() returned without being cancelled")


class RedactSecretsFilter(logging.Filter):
    """No token-shaped string may ever reach ANY log destination (hard rule 6).
    Added after httpx logged Telegram bot URLs (which embed the token) into
    wave.log and the DB log on 2026-08-14."""

    import re as _re

    _PATTERNS = (
        (_re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]{30,}"), "bot<redacted>"),
        (_re.compile(r"\b(?:PK|AK)[A-Z0-9]{16,20}\b"), "<redacted-key>"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = message
            for pattern, replacement in self._PATTERNS:
                redacted = pattern.sub(replacement, redacted)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:  # noqa: S110 — never let redaction break logging
            pass
        return True


def setup_logging() -> None:
    log_dir().mkdir(parents=True, exist_ok=True)
    redact = RedactSecretsFilter()
    handler = logging.handlers.RotatingFileHandler(
        log_dir() / "wave.log", maxBytes=2_000_000, backupCount=5
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(redact)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    stream.addFilter(redact)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(stream)
    # HTTP client libraries log full request URLs (token leak) — errors only.
    # alpaca's stream logger echoes the FULL symbol list on every subscribe
    # change (3+ duplicate lines each) — Wave logs its own concise
    # "now watching X" instead (2026-08-19: log felt "all over
    # the place")
    for noisy in ("httpx", "httpcore", "telegram", "urllib3", "alpaca"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _prepare_glass(window) -> bool:
    """Apple glass, done the way real macOS apps do it (Finder/Notes):
    a full-height native glass panel under the SIDEBAR only; content stays
    opaque. Follows pyqt-liquidglass's documented sequence — prepare before
    show, apply to the widget after. Any failure → solid theme.
    """
    import os

    if sys.platform != "darwin" or os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        return False
    try:
        import pyqt_liquidglass as glass

        # transparent titlebar + full-size content view + hidden title;
        # (also shows the window — harmless, main() shows it again)
        glass.prepare_window_for_glass(window)
        return True
    except Exception:
        logger.exception("liquid glass unavailable — using solid theme")
        return False


def _apply_glass(window) -> bool:
    """Phase 8.3: the WHOLE window is liquid glass — same recipe as
    the approved login window (HUD material blended behind the window).
    Round 3: the NATIVE traffic lights stay, inset over the sidebar."""
    try:
        import pyqt_liquidglass as glass

        glass.apply_glass_to_window(
            window,
            options=glass.GlassOptions(
                material=glass.GlassMaterial.HUD,
                # BEHIND_WINDOW → WITHIN_WINDOW (2026-09-16):
                # the behind-window compositor was the
                # 09-15 display-freeze suspect and drags card repaints;
                # within-window keeps the glass look on a far cheaper path.
                blending_mode=glass.BlendingMode.WITHIN_WINDOW,
            ),
        )
        try:
            # round 4: lights sit at the very TOP (y was 14 — too low)
            glass.setup_traffic_lights_inset(window, x_offset=12, y_offset=4)
        except Exception:
            logger.warning("traffic-lights inset failed (cosmetic only)")
        logger.info("whole-window liquid glass applied")
        return True
    except Exception:
        logger.exception("liquid glass apply failed — window stays solid")
        return False


def _reveal_from_login(window, login, config, runtime) -> None:
    """Fix 3 (2026-08-23): the zoom transition is gone. The old flow
    set the window's geometry BEFORE the native glass preparation restyled
    the frame (transparent titlebar + full-size content view), so the frame
    shifted after the animation — the dashboard JUMPED, and its top edge
    could land pinned above the allowed screen area, which is why dragging
    UP felt blocked until the window was first moved elsewhere.

    New order, jump-proof by construction: prepare the glass frame FIRST,
    then place the final geometry safely inside availableGeometry, then a
    short macOS-style reveal — the window fades in while rising ~14px into
    place (position-only animation: no relayouts, no snapshots)."""
    window.setWindowOpacity(0.0)  # prepare_window_for_glass may show it
    glass_ready = config.glass_enabled and _prepare_glass(window)

    screen = login.screen()
    avail = screen.availableGeometry() if screen is not None else None
    width, height = window.width(), window.height()
    if avail is not None:
        width = min(width, avail.width())
        height = min(height, avail.height())
    target = QRect(0, 0, width, height)
    anchor = avail if avail is not None else login.frameGeometry()
    target.moveCenter(anchor.center())
    if avail is not None:  # never above the menu bar / off any edge
        target.moveLeft(max(target.left(), avail.left()))
        target.moveTop(max(target.top(), avail.top()))
        if target.right() > avail.right():
            target.moveRight(avail.right())
        if target.bottom() > avail.bottom():
            target.moveBottom(avail.bottom())

    start_rect = QRect(target)
    start_rect.translate(0, 14)
    window.setGeometry(start_rect)
    window.show()
    glass_active = glass_ready and _apply_glass(window)
    window.set_glass_mode(glass_active)
    logger.info("Main window shown (glass=%s)", glass_active)

    login_fade = QPropertyAnimation(login, b"windowOpacity", login)
    login_fade.setDuration(180)
    login_fade.setStartValue(1.0)
    login_fade.setEndValue(0.0)
    login_fade.finished.connect(login.close)
    login_fade.start()

    fade_in = QPropertyAnimation(window, b"windowOpacity", window)
    fade_in.setDuration(260)
    fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
    fade_in.setStartValue(0.0)
    fade_in.setEndValue(1.0)
    fade_in.start()

    rise = QPropertyAnimation(window, b"pos", window)
    rise.setDuration(260)
    rise.setEasingCurve(QEasingCurve.Type.OutCubic)
    rise.setStartValue(start_rect.topLeft())
    rise.setEndValue(target.topLeft())
    rise.start()

    runtime["login_fade"] = login_fade
    runtime["reveal_fade"] = fade_in
    runtime["reveal_rise"] = rise


def main() -> int:
    setup_logging()
    # BUILD STAMP (2026-09-23: restarts were hard to verify from the
    # log alone) — the binary's mtime IS the build time, so this one
    # line in the DB log proves exactly which build every restart runs.
    try:
        from datetime import datetime
        from pathlib import Path

        _exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        _built = datetime.fromtimestamp(_exe.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        _built = "unknown"
    logger.info("Wave %s starting — build %s", __version__, _built)

    # No-idle-sleep guard (2026-08-26, school days): while Wave runs,
    # macOS must not idle-sleep it away. caffeinate -i dies with our pid.
    # HONEST LIMIT: a CLOSED lid still sleeps the Mac (no user-space escape) —
    # positions stay protected by the server-side stops, and reconcile +
    # missed-fill polling catch the book up on wake.
    import subprocess

    try:
        subprocess.Popen(  # noqa: S603
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("idle-sleep guard armed (caffeinate)")
    except Exception:
        logger.warning("caffeinate unavailable — idle sleep not suppressed")

    app = QApplication(sys.argv)
    app.setApplicationName("Wave")

    # Single instance (2026-08-19): a quit that hung left a ~2GB zombie
    # process; relaunching on top of it exhausted an 8GB Air's memory and
    # froze the whole Mac. A second launch now refuses instead of stacking.
    from PyQt6.QtCore import QLockFile

    from waveapp.config import support_dir

    support_dir().mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(support_dir() / "wave.lock"))
    if not instance_lock.tryLock(100):
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.warning(
            None,
            "Wave",
            "Wave is already running (or a previous copy is still shutting"
            " down).\nQuit it first — check the Dock or Activity Monitor.",
        )
        logger.warning("second instance refused — lock held")
        return 1
    from PyQt6.QtGui import QFont, QFontDatabase

    if "SF Pro Text" in QFontDatabase.families():
        app.setFont(QFont("SF Pro Text", 13))
    else:  # macOS system font IS San Francisco — graceful fallback
        app.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
    app.setStyleSheet(APP_QSS)

    config = AppConfig.load()
    password_auth = PasswordAuth(config=config)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    from waveapp.ui.login_window import LoginWindow

    login = LoginWindow(password_auth)
    runtime: dict = {}

    def on_authenticated() -> None:
        # DOUBLE-BUILD GUARD (2026-09-14, the doubled-engine incident): the
        # authenticated signal fired twice (auto-login + the login card's
        # own flow) and built TWO engines in one process — every position
        # bought twice, two Telegram pollers fighting. This function must be
        # idempotent no matter how many times ANY path emits the signal.
        if runtime.get("dashboard_built"):
            logging.getLogger("wave").warning(
                "second authenticated signal ignored — dashboard already built"
            )
            return
        runtime["dashboard_built"] = True
        # Build and wire EVERYTHING first (DB, monitor, pages) while the
        # login card is still up — the zoom animation runs LAST. The old
        # order started the zoom and then synchronously opened the DB, so
        # the event loop never painted a frame: Qt animations are
        # wall-clock-based, and by the first repaint the 380ms were over —
        # one small frame, then a teleport (the 8.13 report).
        window = MainWindow(config)

        # a staged backup restore (8.10) swaps in BEFORE the DB opens
        from waveapp.persistence.db import db_path as _db_path
        from waveapp.persistence.restore import apply_staged_restore

        apply_staged_restore(_db_path(TradingMode.PAPER))
        database = Database.for_mode(TradingMode.PAPER)
        db_log_handler = DBLogHandler(database)
        db_log_handler.addFilter(RedactSecretsFilter())
        logging.getLogger().addHandler(db_log_handler)
        logger.info("database ready at %s", database.path.name)
        window.log_page.attach_database(database)  # Phase 8.9
        # Settings (8.10): backup sources + live-apply wiring
        from waveapp.config import config_path as _config_path

        window.settings_page.attach_backup_sources(_config_path(), database.path)

        # paper connect drives the toggle's gray→blue label fill (§8.3 r2)
        toggle = window.top_bar.mode_toggle
        toggle.begin_connect("paper")

        def status_with_toggle(name: str, color: str, tooltip: str) -> None:
            window.status.set_status(name, color, tooltip)
            if name == "Telegram":
                # Settings rework (2026-08-21): live bridge state in the tab
                window.settings_page.set_bridge_status(tooltip)
            if name == "Alpaca":
                from waveapp.ui import theme

                if color == theme.GREEN:
                    toggle.finish_connect("paper")
                elif color == theme.RED:
                    toggle.finish_connect("paper", ok=False)

        monitor = ConnectionMonitor(
            status_with_toggle,
            watchlist=config.watchlist,
            telegram_user_id=config.telegram_user_id,
            database=database,
            on_engine_state=window.on_engine_state,
            on_error=window.show_error,
            on_candidates=window.scanner_page.update_candidates,
            on_scan_status=window.scanner_page.set_status,
            scan_universe_size=config.scan_universe_size,
            scan_interval_seconds=config.scan_interval_seconds,
            on_balance=window.top_bar.set_balance,
            on_trade=window.top_bar.show_trade,
            on_positions=window.positions_page.update_positions,
            on_marks=window.positions_page.update_marks,
            on_performance=window.performance_page.set_performance,
            on_system=lambda data: (
                window.system_page.update_system(data),
                window.ml_page.update_ml(data.get("ml") or {}),
            ),
            on_scanner2_menu=window.scanner_page.update_menu,
            on_scanner2_news=window.scanner_page.ticker.add_news,
            on_market_regime=window.scanner_page.set_regime,
            on_scanner2_event=lambda symbol, kind: (
                window.scanner_page.mind.candidate_promoted(symbol)
                if kind in ("hod_break", "consol_break")
                else window.scanner_page.mind.candidate_found(symbol)
            ),
            on_universe_progress=lambda done, total, label: (
                window.system_page.set_universe_progress(done, total, label),
                window.scanner_page.set_build_progress(done, total, label),
            ),
            on_scoreboard=window.performance_page.update_scoreboard,
        )
        # per-trade graph removal (2026-08-19): the popup's Remove button
        window.performance_page.on_hide_trade = monitor.hide_performance_trade
        # weekly reports (agenda #4): the Reports page reads stored reports
        window.performance_page.reports_provider = monitor.report_for

        def engine_command(name: str) -> None:
            _spawn(monitor.command(name), f"engine command '{name}'")

        def start_requested() -> None:
            # after a kill the Start button is the §4 re-arm path:
            # Touch ID → confirm card → clear kill halt → normal start
            engine = monitor.engine
            if engine is not None and engine.state.value == "killed":
                from waveapp.security.gate import require_gate
                from waveapp.ui.positions_page import _ConfirmPopup

                def confirm() -> None:
                    popup = _ConfirmPopup(
                        window,
                        "Re-arm Wave after the kill switch and allow trading again?",
                        "Re-arm",
                        lambda: _spawn(monitor.re_arm_kill_and_start(), "re-arm after kill"),
                    )
                    popup.show()
                    popup.raise_()

                require_gate("re-arm Wave after the kill switch", confirm)
            else:
                engine_command("start")

        window.top_bar.start_clicked.connect(start_requested)
        window.top_bar.pause_clicked.connect(lambda: engine_command("pause"))
        window.top_bar.stop_clicked.connect(lambda: engine_command("stop"))
        # §4 hard gates (8.11): System-tab kill switch (Touch ID + confirm
        # already passed in the UI) and weekly-loss re-arm
        window.system_page.kill_confirmed.connect(lambda: engine_command("kill"))
        window.system_page.kill_confirmed.connect(
            lambda: window.top_bar.show_alert("KILL SWITCH — flattening everything")
        )
        window.system_page.rearm_requested.connect(monitor.re_arm_weekly)
        # card SELL buttons: each click is its own task — sells run concurrently
        window.positions_page.sell_requested.connect(monitor.sell_position)
        # popup overrides (8.5): tighten behind confirm; order history from DB
        window.positions_page.tighten_requested.connect(monitor.tighten_stop)
        window.positions_page.history_provider = monitor.position_history

        # Settings (8.10 → rework 2026-08-21): live-apply, then CONFIRM back
        # to the spinner with values read from the running engine, and push a
        # fresh System snapshot so the System tab shows the change instantly
        settings = window.settings_page

        def push_system() -> None:
            try:
                window.system_page.update_system(monitor._system_data())
            except Exception:
                logger.debug("system push after settings change failed", exc_info=True)

        def on_risk_changed(values: dict) -> None:
            monitor.apply_risk_limits(values)
            readback = None
            if monitor.engine is not None:
                limits = monitor.engine.risk.limits
                readback = {
                    "risk_per_trade_pct": limits.risk_per_trade_pct,
                    "max_daily_loss_pct": limits.max_daily_loss_pct,
                    "max_weekly_loss_pct": limits.max_weekly_loss_pct,
                    "max_positions": limits.max_positions,
                    "max_notional_pct": limits.max_notional_pct,
                    "impact_participation_pct": limits.impact_participation_pct,
                }
            settings.confirm_risk_applied(readback)
            push_system()

        def on_scanner_changed(values: dict) -> None:
            monitor.apply_scanner_settings(values["universe_size"], values["interval_seconds"])
            settings.confirm_scanner_applied(values["universe_size"], values["interval_seconds"])
            push_system()

        def on_telegram_id_changed(user_id: int) -> None:
            async def run() -> None:
                result = await monitor.apply_telegram_user_id(user_id)
                settings.confirm_telegram_applied(result)
                push_system()

            _spawn(run(), "telegram user-id apply")

        # ML switch (agenda #3 redo, step 1): persist the mode; "active" is
        # refused outright — no validated model exists (§9.2)
        def on_ml_mode(mode: str) -> None:
            if mode == "active":
                # reset the pills first, THEN show the refusal (mode display
                # rewrites the status line) — on MLPage, where the pills live
                # (the System ML sub-page left 2026-08-30; audit A5-1)
                window.ml_page.set_ml_mode_display(AppConfig.load().ml_mode)
                window.ml_page.set_ml_status("refused — no validated model exists yet (§9.2).")
                return
            fresh = AppConfig.load()
            fresh.ml_mode = mode
            fresh.save()
            window.ml_page.set_ml_status(
                "OFF — nothing runs. Shadow gathers data for the ML decision;"
                " it never touches trading."
                if mode == "off"
                else "shadow — data gathering arrives in the next supervised steps"
            )

        window.ml_page.ml_mode_changed.connect(on_ml_mode)
        window.ml_page.set_ml_mode_display(config.ml_mode)  # startup truth

        def on_telegram_mode_changed(mode: str) -> None:
            # A5-10 (audit 2026-09-22): confirm_telegram_mode returns the
            # truth ("confirmed on the phone" vs "bridge not running — saved
            # to config only") — show it on the Settings bridge line so the
            # UI never claims phone-proof while the bridge is down
            async def run() -> None:
                status = await monitor.confirm_telegram_mode(mode)
                settings.set_bridge_status(f"Notifications: {mode.upper()} — {status}")

            _spawn(run(), "telegram mode confirm")

        settings.risk_changed.connect(on_risk_changed)
        settings.scanner_changed.connect(on_scanner_changed)
        settings.trading_changed.connect(lambda _armed: push_system())
        settings.telegram_id_changed.connect(on_telegram_id_changed)
        settings.telegram_mode_changed.connect(on_telegram_mode_changed)

        def telegram_test() -> None:
            async def run() -> None:
                result = await monitor.send_telegram_test()
                settings.telegram_status.setText(result)

            _spawn(run(), "telegram test")

        settings.telegram_test_requested.connect(telegram_test)

        def polygon_test() -> None:
            async def run() -> None:
                import httpx

                from waveapp.research.history import KEYCHAIN_POLYGON_KEY, POLYGON_BASE
                from waveapp.security import secrets as _secrets

                key = _secrets.get_secret(KEYCHAIN_POLYGON_KEY)
                if not key:
                    settings.polygon_status.setText("polygon: no key in Keychain yet")
                    return
                try:
                    async with httpx.AsyncClient(timeout=15.0) as http:
                        response = await http.get(
                            f"{POLYGON_BASE}/v3/reference/tickers",
                            params={"limit": 1, "apiKey": key},
                        )
                    if response.status_code == 200:
                        settings.polygon_status.setText("polygon: connected")
                    else:
                        settings.polygon_status.setText(
                            f"polygon: FAILED — HTTP {response.status_code}"
                        )
                except Exception as exc:
                    settings.polygon_status.setText(f"polygon: FAILED — {exc}")

            _spawn(run(), "polygon test")

        settings.polygon_test_requested.connect(polygon_test)

        def paper_test() -> None:
            async def run() -> None:
                if monitor._adapter is None:
                    settings.key_status["paper"].setText(
                        "paper: not connected — check the keys and wait for a retry"
                    )
                    return
                try:
                    clock = await monitor._adapter.get_clock(TradingMode.PAPER)
                    market = "open" if clock.is_open else "closed"
                    settings.key_status["paper"].setText(f"paper: connected — market {market}")
                except Exception as exc:
                    settings.key_status["paper"].setText(f"paper: FAILED — {exc}")

            _spawn(run(), "paper keys test")

        def live_test() -> None:
            async def run() -> None:
                try:
                    equity, _cash = await live_reader.get_equity()
                    settings.key_status["live"].setText(
                        f"live: connected — equity ${equity:,.2f} (read-only)"
                    )
                except Exception as exc:
                    settings.key_status["live"].setText(f"live: FAILED — {exc}")

            _spawn(run(), "live keys test")

        settings.paper_test_requested.connect(paper_test)
        settings.live_test_requested.connect(live_test)

        # -- live balance view (Phase 8.3 r2): READ-ONLY, Touch ID gated (§4).
        # Live TRADING stays locked until Phase 11 — this only shows equity.
        from waveapp.broker.live_reader import LiveAccountReader

        live_reader = LiveAccountReader()
        toggle.live_enabled = LiveAccountReader.has_keys()
        if not toggle.live_enabled:
            logger.info(
                "live toggle locked — add Keychain entries alpaca_live_key_id /"
                " alpaca_live_secret to enable the live balance view"
            )

        async def switch_to_live() -> None:
            toggle.set_mode("live")
            toggle.begin_connect("live")
            try:
                equity, _cash = await live_reader.get_equity()
            except Exception as exc:
                logger.exception("live account read failed")
                toggle.finish_connect("live", ok=False)
                toggle.set_mode("paper")
                window.show_error(f"live balance unavailable — {exc}")
                return
            toggle.finish_connect("live")
            window.top_bar.set_balance("live", equity)
            window.top_bar.set_display_mode("live")

        def on_live_authorized(ok: bool) -> None:
            if ok:
                _spawn(switch_to_live(), "switch to live balance view")
            else:
                logger.warning("live view denied (Touch ID/password failed)")

        def on_live_requested() -> None:
            from waveapp.security import auth

            if auth.touch_id_available():
                import threading

                def worker() -> None:
                    ok = auth.authenticate_touch_id("show the live account balance")
                    loop.call_soon_threadsafe(on_live_authorized, ok)

                threading.Thread(target=worker, daemon=True).start()
            else:  # §4 password fallback
                from PyQt6.QtWidgets import QInputDialog, QLineEdit

                text, accepted = QInputDialog.getText(
                    window, "Wave", "Password:", QLineEdit.EchoMode.Password
                )
                on_live_authorized(bool(accepted) and password_auth.verify(text))

        toggle.live_requested.connect(on_live_requested)

        async def refresh_live_balance() -> None:
            """While the live balance is displayed, keep it fresh."""
            while True:
                await asyncio.sleep(30)
                if window.top_bar.display_mode != "live":
                    continue
                try:
                    equity, _cash = await live_reader.get_equity()
                    window.top_bar.set_balance("live", equity)
                except Exception:
                    logger.warning("live balance refresh failed", exc_info=True)

        monitor_task = loop.create_task(monitor.run())
        # A5-4 handoff: belt-and-braces — the loop body is supervised
        # per-cycle inside the monitor, but if the TASK itself ever finishes
        # uncancelled, log it loudly instead of freezing silently
        monitor_task.add_done_callback(_monitor_task_died)
        runtime.update(
            window=window,
            database=database,
            db_log_handler=db_log_handler,
            monitor_task=monitor_task,
            live_balance_task=loop.create_task(refresh_live_balance()),
        )

        # -- transition: the dashboard grows out of the login card (8.13) ----
        _reveal_from_login(window, login, config, runtime)

    login.authenticated.connect(on_authenticated)

    # AUTO-LOGIN for automated runs (2026-09-14): launch with
    # WAVE_AUTOLOGIN=<token> where the token equals the Keychain entry
    # `wave_automation_token`. PAPER ONLY by construction — this app has no
    # live path without the Touch ID gates, which an unattended launch can
    # never satisfy; if a live mode is ever active at startup, auto-login
    # refuses. Wrong/missing token falls through to the normal login.
    import os as _os

    auto_token = _os.environ.get("WAVE_AUTOLOGIN", "")
    if auto_token:
        try:
            from waveapp.security.secrets import get_secret as _auto_secret

            expected = _auto_secret("wave_automation_token")
            if expected and auto_token == expected:
                logging.getLogger("wave").info("auto-login accepted (paper only)")
                login.show()  # geometry must exist for the reveal animation
                QTimer.singleShot(50, login.authenticated.emit)
            else:
                logging.getLogger("wave").warning("auto-login REFUSED (bad token)")
                login.show()
        except Exception:
            logging.getLogger("wave").exception("auto-login failed — manual login")
            login.show()
    else:
        login.show()

    with loop:
        loop.run_forever()
        # QUIT WATCHDOG (2026-08-19, the Mac-freeze incident): whatever
        # happens below, this process is DEAD in 8 seconds. The previous
        # quit hung forever (see wait_for note below), left a ~2GB zombie,
        # and relaunching on top of it froze the whole machine.
        import threading

        threading.Timer(8.0, lambda: os._exit(2)).start()

        # Quit path (crash reports 12:42:12/12:42:51): cancel and drain EVERY
        # pending task — actor loops, scanner/positions loops, backfills,
        # notify one-shots. A task left pending here gets finalized during
        # interpreter teardown, after Qt's C++ objects are gone, and its
        # callbacks emit into freed memory (SIGSEGV in pyqtBoundSignal_emit).
        # asyncio.wait — NEVER wait_for(gather): wait_for waits for the
        # cancellation itself to finish, and python-telegram-bot SUPPRESSES
        # CancelledError to attempt a network "graceful shutdown", which
        # hung the quit indefinitely (17:12:53 log). wait() returns after
        # the timeout no matter what the tasks do.
        from waveapp.engine.actor import PositionActor

        PositionActor.app_shutting_down = True  # quiet protective checks
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(Exception):
                loop.run_until_complete(asyncio.wait(pending, timeout=5.0))
        import gc

        gc.collect()  # run task finalizers NOW, not during teardown

    handler = runtime.get("db_log_handler")
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.stop()
    if runtime.get("database") is not None:
        runtime["database"].close()
    logger.info("Wave exited cleanly")
    # Hard exit (2026-08-19): everything that matters is already flushed —
    # tasks drained, DB closed, log handler stopped. Skipping Py_FinalizeEx
    # avoids the whole teardown-GC class of crashes: a task that survived
    # the 5s drain (e.g. blocked in a sync network call on a worker thread)
    # would otherwise be finalized AFTER Qt's C++ objects are gone and emit
    # into freed memory ("Wave quit unexpectedly", 12:58:31 report).
    logging.shutdown()
    os._exit(0)
