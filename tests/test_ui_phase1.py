"""Phase 1 UI tests — run offscreen via pytest-qt."""

import pytest
from PyQt6.QtCore import Qt

from waveapp.config import AppConfig
from waveapp.ui.main_window import MIN_HEIGHT, MIN_WIDTH, MainWindow
from waveapp.ui.sidebar import EXPANDED_WIDTH, RAIL_WIDTH
from waveapp.ui.wave_logo import _STATE_PARAMS, LogoState, WaveLogo

# -- wave logo ---------------------------------------------------------------


def test_all_logo_states_have_params():
    assert set(_STATE_PARAMS) == set(LogoState)


def test_logo_state_transitions(qtbot):
    logo = WaveLogo()
    qtbot.addWidget(logo)
    assert logo.state == LogoState.DISCONNECTED
    logo.set_state(LogoState.ENERGETIC)
    assert logo.state == LogoState.ENERGETIC
    # blend animation converges to the target params
    qtbot.waitUntil(lambda: abs(logo._shown.speed - 0.38) < 0.005, timeout=2000)


def test_logo_paints_every_state(qtbot):
    logo = WaveLogo()
    qtbot.addWidget(logo)
    logo.resize(84, 40)
    for state in LogoState:
        logo.set_state(state)
        pixmap = logo.grab()  # exercises paintEvent
        assert not pixmap.isNull()


def test_params_lerp_midpoint():
    a = _STATE_PARAMS[LogoState.CALM]
    b = _STATE_PARAMS[LogoState.ENERGETIC]
    mid = a.lerp(b, 0.5)
    assert mid.speed == pytest.approx((a.speed + b.speed) / 2)
    assert mid.speed == pytest.approx((a.speed + b.speed) / 2)


# -- main window -------------------------------------------------------------


def test_main_window_structure(qtbot):
    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    assert window.pages.count() == 7  # + ML (2026-08-30); Duel retired 2026-09-21
    assert window.minimumWidth() == MIN_WIDTH
    assert window.minimumHeight() == MIN_HEIGHT
    assert len(window.sidebar.buttons) == 7  # + ML (2026-08-30); Duel retired 2026-09-21
    # the bench still exists — as the appended Settings section
    assert window.settings_page.section_buttons[-1].text() == "Test"
    last = window.settings_page.stack.widget(window.settings_page.stack.count() - 1)
    assert last is window.test_page
    assert len(window.sidebar.dots) == 4


def test_sidebar_switches_pages(qtbot):
    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    assert window.pages.currentIndex() == 0
    window.sidebar.select(3)
    assert window.pages.currentIndex() == 3
    assert window.sidebar.buttons[3].selected
    assert not window.sidebar.buttons[0].selected


def test_resize_clamps_to_minimum(qtbot):
    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    window.show()
    window.resize(300, 200)
    assert window.width() >= MIN_WIDTH
    assert window.height() >= MIN_HEIGHT


def test_sidebar_hover_expands(qtbot):
    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    window.show()
    sidebar = window.sidebar
    assert sidebar.width() == RAIL_WIDTH
    sidebar.enterEvent(None)
    qtbot.waitUntil(lambda: sidebar.width() == EXPANDED_WIDTH, timeout=2000)
    sidebar.leaveEvent(None)
    qtbot.waitUntil(lambda: sidebar.width() == RAIL_WIDTH, timeout=2000)


# -- top bar -----------------------------------------------------------------


def test_engine_state_drives_chrome(qtbot):
    """Phase 5.2: the engine is the source of truth for the top bar."""
    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    bar = window.top_bar

    window.on_engine_state("idle")
    assert bar.start_button.isEnabled() and not bar.pause_stop.isEnabled()

    window.on_engine_state("running")
    assert not bar.start_button.isEnabled() and bar.pause_stop.isEnabled()

    window.on_engine_state("paused")
    assert bar.start_button.action_hint == "Resume"
    assert bar.pause_stop._armed_stop  # next click = Stop

    window.on_engine_state("killed")
    # Start survives a kill as the Touch-ID-gated re-arm path (8.11)
    assert bar.start_button.isEnabled()
    assert bar.start_button.action_hint == "Re-arm"


def test_pause_stop_two_click_semantics(qtbot):
    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    window.show()
    button = window.top_bar.pause_stop
    window.on_engine_state("running")  # enables the pause/stop button

    with qtbot.waitSignal(button.pause_clicked, timeout=1000):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(button.stop_clicked, timeout=1000):
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)


def test_live_toggle_locked(qtbot):
    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    assert not window.top_bar.mode_toggle.live_enabled
    assert window.top_bar.mode_toggle.mode == "paper"


# (the Phase 1 LoginDialog was superseded by ui/login_window.py in Phase 8.2;
#  its tests live in test_login_window.py)


def test_status_dot_note_shows_immediately_on_hover(qtbot):
    """8.6 r3: dots use our OWN rounded note (no sharp Qt tooltip window),
    and they are the ONLY widgets with hover notes."""
    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    window.status.set_status("Data feed", "#16A34A", "Data feed: IEX stream live")
    dot = next(d for d in window.sidebar.dots if d.name == "Data feed")
    dot.enterEvent(None)
    assert dot.note.isVisible()
    assert "IEX stream live" in dot.note.label.text()
    # live-update while visible
    window.status.set_status("Data feed", "#16A34A", "Data feed: last message 3s ago")
    assert "3s ago" in dot.note.label.text()
    dot.leaveEvent(None)
    assert not dot.note.isVisible()
    # nothing else carries a note anymore
    assert window.top_bar.start_button.toolTip() == ""
    assert window.top_bar.pause_stop.toolTip() == ""
    assert window.top_bar.mode_toggle.toolTip() == ""
    assert all(button.toolTip() == "" for button in window.sidebar.buttons)


def test_wave_modal_presents_and_dismisses(qtbot):
    from PyQt6.QtWidgets import QLabel

    from waveapp.ui.modal import WaveModal

    window = MainWindow(AppConfig())
    qtbot.addWidget(window)
    window.show()

    modal = WaveModal(window, "Test Modal", QLabel("hello"))
    dismissed = []
    modal.dismissed.connect(lambda: dismissed.append(True))
    modal.present()
    assert modal.isVisible()
    assert modal.card.isVisible()

    modal.dismiss()
    assert dismissed == [True]


def test_design_tokens_exist():
    from waveapp.ui import theme

    assert theme.FONT_TEXT == "SF Pro Text"
    assert theme.FONT_DISPLAY == "SF Pro Display"
    assert theme.RADIUS_CARD == 12
    assert theme.SPACE_XL == 24
    assert f"{theme.SIZE_BODY}px" in theme.APP_QSS
    assert theme.FONT_DISPLAY in theme.APP_QSS


def test_calendar_probe_labels_dates_correctly(qtbot):
    """2026-08-23: the Test tab proves Wave's calendar knowledge —
    holidays by NAME, early closes, weekends, plain trading days."""
    from waveapp.ui.test_page import TestPage
    from waveapp.ui.top_bar import TopBar

    page = TestPage(TopBar())
    qtbot.addWidget(page)
    # Labor Day 2026 (Mon Sep 7): a named market holiday
    text = page.probe_calendar_date(2026, 9, 7)
    assert "MARKET HOLIDAY" in text and "Labor Day" in text
    # Black Friday 2026 (Nov 27): trading day with an EARLY 13:00 close
    text = page.probe_calendar_date(2026, 11, 27)
    assert "TRADING DAY" in text and "13:00" in text and "EARLY CLOSE" in text
    # a plain Saturday
    assert "WEEKEND" in page.probe_calendar_date(2026, 9, 5)
    # a plain Tuesday: regular 16:00 close, regimes sampled through the day
    text = page.probe_calendar_date(2026, 9, 8)
    assert "TRADING DAY" in text and "16:00" in text and "open_drive" in text
    # the Clear button restores the hint
    page.calendar_result.setText("something")
    page._check_probe_date()  # exercises the click path with today's date
    assert page.calendar_result.text() != "something"
