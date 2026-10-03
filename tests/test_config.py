from waveapp.config import AppConfig


def test_defaults_when_file_missing(tmp_path):
    config = AppConfig.load(tmp_path / "nope.toml")
    assert config.touch_id_enabled is True


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    config = AppConfig(window_width=1500, touch_id_enabled=False)
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.window_width == 1500
    assert loaded.touch_id_enabled is False
    assert loaded.keychain_password_hash_entry == "app_password_hash"  # noqa: S105 — entry name


def test_unknown_keys_preserved(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('future_setting = "kept"\nwindow_width = 1400\n')
    config = AppConfig.load(path)
    assert config.window_width == 1400
    assert config.extra["future_setting"] == "kept"
    config.save(path)
    assert 'future_setting = "kept"' in path.read_text()


def test_config_never_contains_secret_material(tmp_path):
    """The config file holds Keychain entry NAMES only (SPEC.md 0.6)."""
    path = tmp_path / "config.toml"
    AppConfig().save(path)
    text = path.read_text()
    assert "app_password_hash" in text  # the entry name is fine
    assert "argon2" not in text  # no hash material


def test_close_save_does_not_clobber_external_edits(tmp_path):
    """Regression: settings changed outside the app (e.g. telegram_user_id set
    via CLI while Wave ran) must survive the window-geometry save on close."""
    path = tmp_path / "config.toml"
    stale = AppConfig.load(path)  # app loads defaults at startup
    # meanwhile, an external edit lands:
    external = AppConfig.load(path)
    external.telegram_user_id = 6786056094
    external.save(path)
    # app closes and saves only geometry:
    stale.save_window_geometry(1400, 900, path)
    final = AppConfig.load(path)
    assert final.telegram_user_id == 6786056094  # preserved
    assert final.window_width == 1400


# --- A4-5 regression tests (audit 2026-09-22): config.toml integrity ---


def test_save_is_atomic_no_tmp_residue(tmp_path):
    """A4-5(a): save writes via .tmp + os.replace — after it returns, the file
    parses and no .tmp sibling is left behind."""
    import tomllib

    path = tmp_path / "config.toml"
    AppConfig(telegram_mode="on").save(path)
    assert not (tmp_path / "config.toml.tmp").exists()
    with path.open("rb") as f:
        raw = tomllib.load(f)  # must not raise
    assert raw["telegram_mode"] == "on"


def test_string_with_quotes_and_backslash_round_trips(tmp_path):
    """A4-5(b): a value containing quote/backslash used to serialize to invalid
    TOML — save succeeded, every later load raised."""
    path = tmp_path / "config.toml"
    nasty = 'https://x.test/ping?q="a\\b"\ttail'
    config = AppConfig(heartbeat_url=nasty)
    config.extra["future_note"] = 'say "hi"\\'
    config.save(path)
    loaded = AppConfig.load(path)
    assert loaded.heartbeat_url == nasty
    assert loaded.extra["future_note"] == 'say "hi"\\'


def test_load_corrupt_file_returns_defaults_without_clobbering(tmp_path):
    """A4-5(c): torn TOML at startup → defaults, no raise, and the broken file
    is left untouched for forensics (never overwritten with defaults)."""
    path = tmp_path / "config.toml"
    torn = 'telegram_mode = "on"\nwindow_wid'  # truncated mid-write
    path.write_text(torn)
    config = AppConfig.load(path)  # must not raise
    assert config.telegram_mode == "minimum"  # defaults
    assert path.read_text() == torn  # forensics copy intact


def test_load_recovers_from_valid_tmp_sibling(tmp_path):
    """A4-5(c): when config.toml is torn but a complete .tmp from an
    interrupted atomic save exists, load() uses the .tmp snapshot."""
    path = tmp_path / "config.toml"
    path.write_text('telegram_mode = "on"\nwindow_wid')  # torn main file
    (tmp_path / "config.toml.tmp").write_text('telegram_mode = "on"\nwindow_width = 1440\n')
    config = AppConfig.load(path)
    assert config.telegram_mode == "on"
    assert config.window_width == 1440


def test_load_corrupt_file_and_corrupt_tmp_falls_back_to_defaults(tmp_path):
    """A4-5(c): both the main file AND the .tmp torn → still no raise."""
    path = tmp_path / "config.toml"
    path.write_text("telegram_mo")
    (tmp_path / "config.toml.tmp").write_text("window_wi")
    config = AppConfig.load(path)
    assert config.telegram_mode == "minimum"


def test_ml_mode_defaults_off_and_round_trips(tmp_path):
    """Agenda #3 redo (2026-08-23): ML is OFF unless it is flipped manually."""
    from waveapp.config import AppConfig

    assert AppConfig().ml_mode == "off"
    path = tmp_path / "c.toml"
    config = AppConfig(ml_mode="shadow")
    config.save(path)
    assert AppConfig.load(path).ml_mode == "shadow"


def test_shorts_enabled_defaults_false(tmp_path):
    """S0 rule zero (2026-09-23): the short side ships DARK — the dataclass
    default, a missing file and a file without the key all mean False, and a
    save writes the false explicitly so the file states the truth."""
    from waveapp.config import AppConfig

    assert AppConfig().shorts_enabled is False
    assert AppConfig.load(tmp_path / "nope.toml").shorts_enabled is False
    path = tmp_path / "config.toml"
    AppConfig().save(path)
    assert "shorts_enabled = false" in path.read_text()
    assert AppConfig.load(path).shorts_enabled is False
