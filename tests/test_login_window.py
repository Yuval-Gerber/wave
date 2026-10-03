"""Phase 8.2 (rounds 3–5): the redesigned frameless glass login window."""

import pytest

from waveapp.config import AppConfig
from waveapp.security.auth import PasswordAuth
from waveapp.ui.login_window import (
    COMPACT_SIZE,
    LARGE_SIZE,
    CircleButton,
    HeroCanvas,
    LoginWindow,
    PasswordBar,
    word_aspect,
    word_segments,
)


@pytest.fixture
def no_touch_id(monkeypatch):
    monkeypatch.setattr("waveapp.security.auth.touch_id_available", lambda: False)


def _window(fake_keychain, password=None) -> LoginWindow:
    password_auth = PasswordAuth(config=AppConfig())
    if password:
        password_auth.set_password(password)
    return LoginWindow(password_auth)


# -- hero ---------------------------------------------------------------------


def test_hero_paints_through_the_cycle(qtbot):
    hero = HeroCanvas()
    qtbot.addWidget(hero)
    hero.resize(330, 200)
    for phase in (0.05, 0.2, 0.41, 0.5, 0.8, 0.96):
        hero._phase = phase
        assert not hero.grab().isNull()
    from PyQt6.QtCore import QPointF

    hero._cursor = QPointF(160, 60)
    assert not hero.grab().isNull()


def test_word_is_traced_handwriting():
    segments = word_segments()
    assert len(segments) >= 1  # pen stroke(s) from the SVG
    assert sum(len(s) for s in segments) > 500  # smooth Bézier sampling
    assert 2.5 < word_aspect() < 5.5


def test_word_and_wave_share_timing_fifo(qtbot):
    """Same (start,end) window: they start together, end together, and the
    first-drawn part disappears first (FIFO), no retracting."""
    hero = HeroCanvas()
    qtbot.addWidget(hero)
    hero.resize(330, 200)
    erase_path = hero._word_path(0.4, 1.0)
    full_path = hero._word_path(0.0, 1.0)
    assert erase_path is not None and full_path is not None
    assert erase_path.elementCount() < full_path.elementCount()
    start_of_erase = erase_path.elementAt(0)
    start_of_full = full_path.elementAt(0)
    assert (start_of_erase.x, start_of_erase.y) != (start_of_full.x, start_of_full.y)


# -- buttons & bar ------------------------------------------------------------


def test_circle_buttons_render_and_click(qtbot):
    for icon in ("fingerprint", "user"):
        button = CircleButton(icon)
        qtbot.addWidget(button)
        clicks = []
        button.clicked.connect(lambda clicks=clicks: clicks.append(True))
        assert not button.grab().isNull()
        from PyQt6.QtCore import Qt

        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        assert clicks == [True]


def test_password_bar_expand_submit_collapse(qtbot):
    bar = PasswordBar(first_run=False)
    qtbot.addWidget(bar)
    submitted = []
    bar.submitted.connect(lambda pw, cf: submitted.append((pw, cf)))
    bar.expand(280)
    qtbot.waitUntil(lambda: bar.width() >= 279, timeout=2000)
    assert not bar.grab().isNull()  # paints icon, arc, check circle
    bar.password.setText("hunter2hunter2")
    bar._submit()
    assert submitted == [("hunter2hunter2", "")]
    bar.collapse()  # retracts INTO the circle, then hides
    qtbot.waitUntil(lambda: bar.isHidden(), timeout=2000)


def test_open_bar_hides_buttons_and_collapse_restores(qtbot, fake_keychain, no_touch_id):
    window = _window(fake_keychain, password="hunter2hunter2")
    qtbot.addWidget(window)
    window.show()
    window.open_password_bar()
    assert window.user_button.isHidden()
    assert window.touch_button.isHidden()
    window.bar.collapse()
    qtbot.waitUntil(lambda: not window.user_button.isHidden(), timeout=2000)


# -- auth flows ---------------------------------------------------------------


def test_login_success_and_wrong_password_flashes(qtbot, fake_keychain, no_touch_id, monkeypatch):
    window = _window(fake_keychain, password="hunter2hunter2")
    qtbot.addWidget(window)
    flashes = []
    monkeypatch.setattr(window, "flash", lambda: flashes.append(True))
    authed = []
    window.authenticated.connect(lambda: authed.append(True))

    window._on_password_submitted("wrong", "")
    assert authed == []
    assert flashes == [True]  # the red lamp behind the hero, not a message

    window._on_password_submitted("hunter2hunter2", "")
    assert authed == [True]


def test_login_first_run_creates_password(qtbot, fake_keychain, no_touch_id, monkeypatch):
    window = _window(fake_keychain)
    qtbot.addWidget(window)
    assert window.first_run
    monkeypatch.setattr(window, "flash", lambda: None)
    authed = []
    window.authenticated.connect(lambda: authed.append(True))
    window._on_password_submitted("hunter2hunter2", "different")
    assert authed == []
    window._on_password_submitted("hunter2hunter2", "hunter2hunter2")
    assert authed == [True]


def test_no_lockout_after_many_failures(qtbot, fake_keychain, no_touch_id, monkeypatch):
    """Round 5: no 60s lockout — wrong guesses only flash; the right password
    always gets in."""
    window = _window(fake_keychain, password="hunter2hunter2")
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "flash", lambda: None)
    authed = []
    window.authenticated.connect(lambda: authed.append(True))
    for _ in range(5):
        window._on_password_submitted("wrong", "")
    assert window.bar.password.isEnabled()
    window._on_password_submitted("hunter2hunter2", "")
    assert authed == [True]


def test_flash_lamp_lives_behind_the_hero(qtbot, fake_keychain, no_touch_id):
    """Round 5: the red lamp glows behind the wave + word (in the hero
    canvas), not over the whole card."""
    window = _window(fake_keychain, password="hunter2hunter2")
    qtbot.addWidget(window)
    window.hero.set_flash_alpha(64)
    assert window.hero._flash_alpha == 64
    assert not window.hero.grab().isNull()  # paints with the lamp on
    window.hero.set_flash_alpha(0)


def test_shake_returns_to_origin(qtbot, fake_keychain, no_touch_id):
    window = _window(fake_keychain, password="hunter2hunter2")
    qtbot.addWidget(window)
    window.show()
    origin = window.pos()
    window.shake()
    qtbot.waitUntil(lambda: window.pos() == origin, timeout=2000)


# -- window controls ----------------------------------------------------------


def test_traffic_lights_and_zoom_toggle(qtbot, fake_keychain, no_touch_id):
    window = _window(fake_keychain, password="hunter2hunter2")
    qtbot.addWidget(window)
    window.show()
    assert (window.width(), window.height()) == COMPACT_SIZE
    window.toggle_zoom()
    qtbot.waitUntil(lambda: (window.width(), window.height()) == LARGE_SIZE, timeout=2000)
    window.toggle_zoom()
    qtbot.waitUntil(lambda: (window.width(), window.height()) == COMPACT_SIZE, timeout=2000)
