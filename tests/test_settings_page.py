"""Phase 8.10: the Settings tab — every section really reads/writes."""

from pathlib import Path

from waveapp.config import AppConfig
from waveapp.ui.settings_page import SECTIONS, SettingsPage


def _page(qtbot, tmp_path, **overrides) -> tuple[SettingsPage, Path]:
    config_path = tmp_path / "config.toml"
    config = AppConfig(**overrides)
    config.save(config_path)
    page = SettingsPage(config_path=config_path)
    qtbot.addWidget(page)
    return page, config_path


def test_sections_and_current_values_load(qtbot, tmp_path, fake_keychain):
    page, _path = _page(qtbot, tmp_path, telegram_user_id=123, scan_universe_size=250)
    assert page.stack.count() == len(SECTIONS)
    assert page.scan_size.text() == "250"
    assert page.telegram_id_edit.text() == "123"
    assert page.risk_per_trade.text() == "1"
    assert page.risk_impact.text() == "0.5"  # market-impact cap (rework)
    assert "open_drive" in page.params_label.text()  # read-only exit params
    # champion-aware rendering — never the raw 999 sentinels
    assert "999" not in page.params_label.text()
    assert "trail 3.5" in page.params_label.text()  # old champion restored 2026-09-04
    assert "999" not in page.params_label.text()
    assert "$15" in page.floor_label.text()  # the champion price floor ($15, 2026-08-24)
    assert page.auto_trade_check.isEnabled()  # armed/armable since phase-10.14


def test_risk_gated_then_saves_and_emits(qtbot, tmp_path, fake_keychain):
    page, path = _page(qtbot, tmp_path)
    applied = []
    page.risk_changed.connect(applied.append)
    page._save_risk()  # locked → refused
    assert applied == []
    page._unlock_risk()  # what Touch ID success calls
    page.select_section(1)  # Risk
    assert not page.risk_editors_box.isHidden()
    assert page.risk_locked_box.isHidden()
    page.risk_daily.setText("2.5")
    page.risk_positions.setText("2")
    page._save_risk()
    assert applied and applied[0]["max_daily_loss_pct"] == 2.5
    saved = AppConfig.load(path)
    assert saved.max_daily_loss_pct == 2.5  # ACTUALLY changed on disk
    assert saved.max_positions == 2
    # nonsense is refused
    page.risk_daily.setText("banana")
    page._save_risk()
    assert AppConfig.load(path).max_daily_loss_pct == 2.5


def test_scanner_saves_and_emits(qtbot, tmp_path, fake_keychain):
    page, path = _page(qtbot, tmp_path)
    emitted = []
    page.scanner_changed.connect(emitted.append)
    page.scan_size.setText("300")
    page.scan_interval.setText("90")
    page.watchlist_edit.setText("spy, qqq , aapl")
    page._save_scanner()
    saved = AppConfig.load(path)
    assert saved.scan_universe_size == 300
    assert saved.scan_interval_seconds == 90
    assert saved.watchlist == ["SPY", "QQQ", "AAPL"]
    assert emitted == [{"universe_size": 300, "interval_seconds": 90}]


def test_keys_save_to_keychain_only(qtbot, tmp_path, fake_keychain):
    page, path = _page(qtbot, tmp_path)
    key_edit, secret_edit = page.key_fields["paper"]
    key_edit.setText("fake-key-id-for-test")
    secret_edit.setText("supersecretvalue")
    page._save_keys("paper")
    assert _vault(fake_keychain)["alpaca_paper_key_id"] == "fake-key-id-for-test"
    assert _vault(fake_keychain)["alpaca_paper_secret"] == "supersecretvalue"  # noqa: S105
    assert key_edit.text() == "" and secret_edit.text() == ""  # fields wiped
    assert "saved to Keychain" in page.key_status["paper"].text()
    # nothing secret ever lands in the config file
    assert "supersecretvalue" not in path.read_text()


def test_telegram_saves(qtbot, tmp_path, fake_keychain):
    page, path = _page(qtbot, tmp_path)
    page.telegram_id_edit.setText("6786056094")
    page._save_telegram()
    assert AppConfig.load(path).telegram_user_id == 6786056094


def test_backup_roundtrip_and_staged_restore(qtbot, tmp_path, fake_keychain):
    page, config_path = _page(qtbot, tmp_path, telegram_user_id=42)
    db_path = tmp_path / "wave_paper.db"
    db_path.write_bytes(b"SQLite format 3\x00fakedata")
    page.attach_backup_sources(config_path, db_path)

    backup = tmp_path / "backup.zip"
    assert page.export_backup(str(backup))
    assert backup.exists()

    # mutate, then restore: config comes back, DB is STAGED (not overwritten
    # under a live WAL connection)
    page._save_fields(telegram_user_id=0)
    db_path.write_bytes(b"SQLite format 3\x00changed")
    assert page.restore_backup(str(backup))
    assert AppConfig.load(config_path).telegram_user_id == 42
    assert db_path.read_bytes().endswith(b"changed")  # live file untouched
    staged = db_path.with_suffix(".db.restored")
    assert staged.exists() and staged.read_bytes().endswith(b"fakedata")


def test_monitor_live_apply_swaps_frozen_limits():
    from types import SimpleNamespace

    from waveapp.engine.connection_monitor import ConnectionMonitor
    from waveapp.engine.risk import RiskEngine

    monitor = ConnectionMonitor(on_status=lambda *a: None)
    monitor.engine = SimpleNamespace(risk=RiskEngine())
    monitor.apply_risk_limits({"max_daily_loss_pct": 2.0, "max_positions": 5})
    assert monitor.engine.risk.limits.max_daily_loss_pct == 2.0
    assert monitor.engine.risk.limits.max_positions == 5
    assert monitor.engine.risk.limits.risk_per_trade_pct == 1.0  # untouched
    # A3-10 (audit 2026-09-22): the WAVE-2 book fields live-apply too —
    # they used to silently no-op until the next engine start
    monitor.apply_risk_limits({"max_total_risk_pct": 8.0, "max_positions_hard": 20})
    assert monitor.engine.risk.limits.max_total_risk_pct == 8.0
    assert monitor.engine.risk.limits.max_positions_hard == 20
    assert monitor.engine.risk.limits.max_positions == 5  # earlier apply kept
    monitor.apply_scanner_settings(300, 90)
    assert monitor._scan_universe_size == 300
    assert monitor._scan_interval == 90


def test_test_bench_attaches_as_settings_section(qtbot, tmp_path, fake_keychain):
    from PyQt6.QtWidgets import QLabel

    page, _path = _page(qtbot, tmp_path)
    bench = QLabel("bench")
    page.attach_test_page(bench)
    assert page.stack.count() == len(SECTIONS) + 1
    assert page.section_buttons[-1].text() == "Test"
    page.select_section(len(SECTIONS))  # the appended Test section
    assert page.stack.currentWidget() is bench


def test_bot_token_gated_save_to_keychain_only(qtbot, tmp_path, monkeypatch, fake_keychain):
    from waveapp.ui.settings_page import SettingsPage

    reasons = []

    def fake_gate(reason, on_success):
        reasons.append(reason)
        on_success()

    from waveapp.security import gate

    monkeypatch.setattr(gate, "require_gate", fake_gate)
    config_path = tmp_path / "config.toml"
    AppConfig().save(config_path)
    page = SettingsPage(config_path=config_path)
    qtbot.addWidget(page)
    assert "NOT set" in page.telegram_token_status.text()

    page._save_token_requested()  # empty: refused before the gate
    assert reasons == []
    page.telegram_token_edit.setText("fake-bot-token-for-test")
    page._save_token_requested()
    assert reasons == ["save the Telegram bot token"]
    assert _vault(fake_keychain)["telegram_bot_token"] == "fake-bot-token-for-test"  # noqa: S105
    assert page.telegram_token_edit.text() == ""  # wiped
    assert "fake-bot-token-for-test" not in config_path.read_text()


# -- Settings rework (2026-08-21): spinner lifecycle, gates, restores ---------


def _gate_passthrough(monkeypatch):
    reasons = []

    def fake_gate(reason, on_success):
        reasons.append(reason)
        on_success()

    from waveapp.security import gate

    monkeypatch.setattr(gate, "require_gate", fake_gate)
    return reasons


def test_save_spinner_lifecycle(qtbot):
    from waveapp.ui.settings_page import _SaveSpinner

    spinner = _SaveSpinner()
    qtbot.addWidget(spinner)
    spinner.min_spin_ms = 0
    spinner.spin("working…")
    assert spinner.state == "spin"
    spinner.ok("applied")
    assert spinner.state == "ok" and spinner.text() == "applied"
    spinner.pending("saved — applies on the next launch")
    assert spinner.state == "pending"
    spinner.fail("FAILED")
    assert spinner.state == "fail"
    # the QLabel-compatible API classifies by wording
    spinner.setText("paper: connected — market open")
    assert spinner.state == "ok"
    spinner.setText("polygon: FAILED — boom")
    assert spinner.state == "fail"


def test_auto_trade_arm_is_gated_disarm_is_free(qtbot, tmp_path, monkeypatch, fake_keychain):
    reasons = _gate_passthrough(monkeypatch)
    page, path = _page(qtbot, tmp_path, auto_trade=False)
    page.trading_status.min_spin_ms = 0
    armed = []
    page.trading_changed.connect(armed.append)
    page._auto_trade_clicked(True)  # arming → Touch ID first
    assert reasons == ["arm auto-trading"]
    assert AppConfig.load(path).auto_trade is True
    assert page.auto_trade_check.isChecked()
    assert armed == [True]
    page._auto_trade_clicked(False)  # disarming is always free
    assert reasons == ["arm auto-trading"]  # no second gate
    assert AppConfig.load(path).auto_trade is False
    assert armed == [True, False]


def test_risk_relocks_on_leaving_section_and_after_save(qtbot, tmp_path, fake_keychain):
    from waveapp.ui.settings_page import _RISK_SECTION

    page, path = _page(qtbot, tmp_path)
    page.risk_status.min_spin_ms = 0
    page._unlock_risk()
    page.select_section(_RISK_SECTION)
    assert not page.risk_editors_box.isHidden()
    page.select_section(0)  # §4: walking away closes the gate
    assert page.risk_editors_box.isHidden() and not page._risk_unlocked
    page._unlock_risk()
    page.risk_daily.setText("2")
    page._save_risk()  # saving also closes the gate
    assert AppConfig.load(path).max_daily_loss_pct == 2.0
    assert page.risk_editors_box.isHidden() and not page._risk_unlocked


def test_risk_save_includes_impact_cap_and_readback_confirm(qtbot, tmp_path, fake_keychain):
    page, path = _page(qtbot, tmp_path)
    page.risk_status.min_spin_ms = 0
    applied = []
    page.risk_changed.connect(applied.append)
    page._unlock_risk()
    page.risk_impact.setText("0.25")
    page._save_risk()
    assert applied[0]["impact_participation_pct"] == 0.25
    assert AppConfig.load(path).impact_participation_pct == 0.25
    assert page.risk_status.state == "spin"  # waiting for the engine's ack
    page.confirm_risk_applied(
        {
            "risk_per_trade_pct": 1.0,
            "max_daily_loss_pct": 3.0,
            "max_positions": 3,
            "impact_participation_pct": 0.25,
        }
    )
    assert page.risk_status.state == "ok"
    assert "0.25% of daily volume" in page.risk_status.text()


def test_restore_defaults_risk_and_scanner(qtbot, tmp_path, fake_keychain):
    page, path = _page(
        qtbot, tmp_path, max_daily_loss_pct=1.5, scan_universe_size=99, scan_interval_seconds=7
    )
    page.risk_status.min_spin_ms = 0
    page.scanner_status.min_spin_ms = 0
    page._unlock_risk()
    page._restore_risk_defaults()
    saved = AppConfig.load(path)
    assert saved.max_daily_loss_pct == 3.0 and saved.max_positions == 6  # 6-slot book, 2026-08-25
    page._restore_scanner_defaults()
    saved = AppConfig.load(path)
    assert saved.scan_universe_size == 1500 and saved.scan_interval_seconds == 120
    page.confirm_scanner_applied(1500, 120)
    assert page.scanner_status.state == "ok"


def test_scanner_watchlist_change_lands_amber(qtbot, tmp_path, fake_keychain):
    page, _path = _page(qtbot, tmp_path)
    page.scanner_status.min_spin_ms = 0
    page.watchlist_edit.setText("SPY, IWM")  # differs from the loaded list
    page._save_scanner()
    page.confirm_scanner_applied(1500, 120)
    assert page.scanner_status.state == "pending"  # honest: next launch
    assert "next launch" in page.scanner_status.text()


def test_telegram_restore_clears_id_and_backup_restore_is_gated(
    qtbot, tmp_path, monkeypatch, fake_keychain
):
    reasons = _gate_passthrough(monkeypatch)
    page, path = _page(qtbot, tmp_path, telegram_user_id=42)
    page.telegram_status.min_spin_ms = 0
    page._restore_telegram_defaults()
    assert AppConfig.load(path).telegram_user_id == 0
    # restore-from-backup goes through Touch ID before the confirm card
    from PyQt6.QtWidgets import QFileDialog

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("fake.zip", ""))
    )
    page._restore_backup_clicked()
    assert "restore a Wave backup" in reasons


def test_backup_export_is_wal_safe_and_stamps_the_date(qtbot, tmp_path, fake_keychain):
    import sqlite3
    import zipfile as zf

    page, config_path = _page(qtbot, tmp_path)
    page.backup_status.min_spin_ms = 0
    db_path = tmp_path / "wave_paper.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()  # rows may sit in the -wal sidecar — the raw file misses them
    page.attach_backup_sources(config_path, db_path)
    backup = tmp_path / "b.zip"
    assert page.export_backup(str(backup))
    with zf.ZipFile(backup) as bundle, bundle.open("wave_paper.db") as src:
        snapshot = tmp_path / "check.db"
        snapshot.write_bytes(src.read())
    rows = sqlite3.connect(snapshot).execute("SELECT x FROM t").fetchall()
    conn.close()
    assert rows == [(7,)]  # the WAL row made it into the backup
    assert AppConfig.load(config_path).last_backup_at != ""
    assert "Last backup" in page.last_backup_label.text()


def _vault(fake_keychain: dict) -> dict:
    """Read the single-item VAULT (2026-09-02) the way the app stores it."""
    import json

    return json.loads(fake_keychain.get(("Wave", "vault"), "{}"))


# -- A4-5 leg 4 (audit 2026-09-22): stale-widget section saves ----------------
# The page populated widgets ONCE at construction; a section Save hours later
# wrote app-start values over fields changed elsewhere meanwhile. showEvent
# now reloads from the file the moment the page becomes visible.


def test_show_event_reloads_widgets_from_file(qtbot, tmp_path, fake_keychain):
    page, path = _page(qtbot, tmp_path, scan_universe_size=250, telegram_user_id=123)
    assert page.scan_size.text() == "250"
    # another process / another section saved meanwhile (fresh-load→save)
    fresh = AppConfig.load(path)
    fresh.scan_universe_size = 400
    fresh.max_daily_loss_pct = 2.5
    fresh.telegram_user_id = 777
    fresh.save(path)
    assert page.scan_size.text() == "250"  # widgets still stale while hidden
    page.show()  # navigating to the Settings tab → showEvent → reload
    assert page.scan_size.text() == "400"
    assert page.risk_daily.text() == "2.5"
    assert page.telegram_id_edit.text() == "777"
    # sanity: a Save now writes the FRESH sibling values, not app-start ones
    saved = AppConfig.load(path)
    assert saved.scan_universe_size == 400


def test_show_reload_skipped_while_save_ack_mid_flight(qtbot, tmp_path, fake_keychain):
    page, path = _page(qtbot, tmp_path, scan_universe_size=250)
    page.telegram_status.spin("saved — reconnecting the bridge…")  # ack pending
    fresh = AppConfig.load(path)
    fresh.scan_universe_size = 400
    fresh.save(path)
    page.show()
    assert page.scan_size.text() == "250"  # guarded — never reload mid-confirm
    page.telegram_status.clear()  # ack landed (fallback timers guarantee this)
    page.hide()
    page.show()
    assert page.scan_size.text() == "400"  # next visibility picks it up
