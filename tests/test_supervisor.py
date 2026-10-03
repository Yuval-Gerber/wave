"""WAVE 2 item 1 — the everything-supervisor: crashed and hung loops
resurrect within one check cycle (chaos test, born 2026-09-16 when the
Mac's sleep killed the scan loop silently for 2.5 hours)."""

from __future__ import annotations

import asyncio
import time

import pytest

import waveapp.engine.supervisor as sup_mod
from waveapp.engine.supervisor import Supervisor


@pytest.fixture
def fast_checks(monkeypatch):
    monkeypatch.setattr(sup_mod, "CHECK_SECONDS", 0.05)


async def test_crashed_loop_resurrects(fast_checks):
    lives = []

    async def loop():
        lives.append(time.time())
        raise RuntimeError("boom")  # crashes immediately

    sup = Supervisor()
    task = asyncio.ensure_future(loop())
    await asyncio.sleep(0.01)  # let it crash
    sup.register("crashy", task, lambda: asyncio.ensure_future(loop()))
    sup.start()
    await asyncio.sleep(0.12)  # one check cycle+
    await sup.stop()
    assert len(lives) >= 2  # original + at least one resurrection
    name, healthy, resurrections = sup.status()[0]
    assert name == "crashy" and resurrections >= 1


async def test_hung_loop_is_cancelled_and_resurrected(fast_checks):
    starts = []
    beat = {"t": time.time() - 999.0}  # stale from the start

    async def loop():
        starts.append(time.time())
        await asyncio.sleep(3600)  # hung forever, never beats

    sup = Supervisor()
    task = asyncio.ensure_future(loop())
    sup.register(
        "hangy",
        task,
        lambda: asyncio.ensure_future(loop()),
        beat=lambda: beat["t"],
        stale_after=1.0,
        active=lambda: True,
    )
    sup.start()
    await asyncio.sleep(0.12)
    await sup.stop()
    assert len(starts) >= 2  # cancelled and restarted
    assert sup.status()[0][2] >= 1
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task():
            t.cancel()


async def test_healthy_loop_untouched(fast_checks):
    async def loop():
        while True:
            await asyncio.sleep(0.01)

    sup = Supervisor()
    task = asyncio.ensure_future(loop())
    sup.register("healthy", task, lambda: asyncio.ensure_future(loop()))
    sup.start()
    await asyncio.sleep(0.12)
    await sup.stop()
    assert sup.status()[0][1] is True and sup.status()[0][2] == 0
    task.cancel()
