"""Phase 9: Backup/Restore migration proof — the full old-Mac → new-Mac cycle."""

from pathlib import Path

from waveapp.config import AppConfig
from waveapp.persistence.restore import apply_staged_restore


def test_staged_restore_swaps_in_before_db_opens(tmp_path):
    db = tmp_path / "wave_paper.db"
    db.write_bytes(b"live-db")
    (tmp_path / "wave_paper.db-wal").write_bytes(b"wal")
    (tmp_path / "wave_paper.db-shm").write_bytes(b"shm")
    staged = tmp_path / "wave_paper.db.restored"
    staged.write_bytes(b"restored-db")

    assert apply_staged_restore(db) is True
    assert db.read_bytes() == b"restored-db"
    assert not staged.exists()
    # WAL sidecars of the replaced database are gone — a stale WAL against a
    # different database file would corrupt it on open
    assert not (tmp_path / "wave_paper.db-wal").exists()
    assert not (tmp_path / "wave_paper.db-shm").exists()


def test_staged_restore_is_a_noop_without_a_staged_file(tmp_path):
    db = tmp_path / "wave_paper.db"
    db.write_bytes(b"live-db")
    assert apply_staged_restore(db) is False
    assert db.read_bytes() == b"live-db"


def test_full_migration_cycle_old_mac_to_new_mac(qtbot, tmp_path, fake_keychain):
    """Export on the 'old Mac', restore on a 'new Mac', swap at relaunch."""
    from waveapp.ui.settings_page import SettingsPage

    # -- old Mac: real config + db, export a backup
    old = tmp_path / "old"
    old.mkdir()
    old_config = old / "config.toml"
    AppConfig(telegram_user_id=6786056094, scan_universe_size=250).save(old_config)
    old_db = old / "wave_paper.db"
    old_db.write_bytes(b"SQLite format 3\x00old-mac-history")
    page = SettingsPage(config_path=old_config)
    qtbot.addWidget(page)
    page.attach_backup_sources(old_config, old_db)
    backup = tmp_path / "wave-backup.zip"
    assert page.export_backup(str(backup))

    # -- backup contains NO secrets (they live only in Keychain)
    import zipfile

    names = zipfile.ZipFile(backup).namelist()
    assert set(names) == {"config.toml", "wave_paper.db"}

    # -- new Mac: fresh support dir, defaults everywhere
    new = tmp_path / "new"
    new.mkdir()
    new_config = new / "config.toml"
    AppConfig().save(new_config)
    new_db = new / "wave_paper.db"
    new_db.write_bytes(b"SQLite format 3\x00fresh-empty")
    fresh = SettingsPage(config_path=new_config)
    qtbot.addWidget(fresh)
    fresh.attach_backup_sources(new_config, new_db)
    assert fresh.restore_backup(str(backup))

    # config applies immediately; db is staged, live file untouched
    restored_config = AppConfig.load(new_config)
    assert restored_config.telegram_user_id == 6786056094
    assert restored_config.scan_universe_size == 250
    assert new_db.read_bytes().endswith(b"fresh-empty")

    # relaunch: the swap runs before the Database opens (app.py path)
    assert apply_staged_restore(new_db) is True
    assert new_db.read_bytes().endswith(b"old-mac-history")


def test_bundle_spec_covers_every_runtime_resource():
    """Every Path(__file__)-relative resource the app loads must be listed
    in wave.spec's datas — a missing one only explodes inside the built app."""
    spec = Path("wave.spec").read_text()
    for resource in (
        "waveapp/ui/wave_stroke.json",
        "waveapp/ui/word_stroke.json",
        "waveapp/ui/check.svg",
        "waveapp/persistence/migrations",
    ):
        assert resource in spec, f"{resource} missing from wave.spec datas"


def test_version_single_source():
    """pyproject + spec + app all read waveapp.__version__ (Phase 9)."""
    import importlib.metadata

    from waveapp import __version__

    assert importlib.metadata.version("waveapp") == __version__
    spec = Path("wave.spec").read_text()
    assert "from waveapp import __version__" in spec
    assert '"CFBundleShortVersionString": __version__' in spec
