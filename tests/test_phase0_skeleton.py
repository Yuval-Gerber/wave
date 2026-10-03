"""Phase 0 sanity tests: the skeleton imports, the toolchain works, and the
package name does not collide with the standard library."""

import asyncio
import importlib


def test_package_imports_with_version():
    waveapp = importlib.import_module("waveapp")
    assert waveapp.__version__
    assert waveapp.APP_NAME == "Wave"


def test_all_subpackages_import():
    for name in (
        "waveapp.engine",
        "waveapp.broker",
        "waveapp.data",
        "waveapp.persistence",
        "waveapp.telegram",
        "waveapp.security",
        "waveapp.ui",
    ):
        assert importlib.import_module(name) is not None


def test_stdlib_wave_is_not_shadowed():
    """The package is deliberately named waveapp; stdlib `wave` must stay intact."""
    wave = importlib.import_module("wave")
    assert hasattr(wave, "open"), "stdlib wave module is shadowed"


async def test_asyncio_harness_works():
    """pytest-asyncio auto mode runs bare async tests (engine phases rely on this)."""
    await asyncio.sleep(0)
    assert asyncio.get_running_loop() is not None
