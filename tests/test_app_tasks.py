"""Audit 2026-09-22, Lane U: app.py fire-and-forget task plumbing.

A5-6 class — asyncio holds only weak refs to tasks, so app.py's UI-triggered
ensure_future sites route through _spawn (strong ref + loud failure log);
A5-4 handoff — monitor_task gets a done-callback that screams if the
supervised monitor loop ever finishes uncancelled.
"""

import asyncio
import contextlib
import gc
import logging

import pytest

from waveapp import app as wave_app


async def test_spawn_holds_strong_ref_until_done():
    done = asyncio.Event()

    async def worker():
        await done.wait()
        return "ok"

    task = wave_app._spawn(worker(), "manual sell")
    assert task is not None
    assert task in wave_app._UI_TASKS  # the strong ref the loop doesn't keep
    gc.collect()  # a GC pass mid-flight must not collect the task
    assert not task.done()
    done.set()
    assert await task == "ok"
    await asyncio.sleep(0)  # let the done-callback run
    assert task not in wave_app._UI_TASKS  # and the ref is dropped after


async def test_spawn_logs_swallowed_exception(caplog):
    async def boom():
        raise RuntimeError("kaput")

    with caplog.at_level(logging.ERROR, logger="wave"):
        task = wave_app._spawn(boom(), "polygon test")
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
    assert task not in wave_app._UI_TASKS
    messages = [r.getMessage() for r in caplog.records]
    assert any("polygon test" in m and "kaput" in m for m in messages)


async def test_monitor_died_callback_screams_on_exception(caplog):
    async def dies():
        raise ValueError("teardown blew up")

    task = asyncio.get_running_loop().create_task(dies())
    task.add_done_callback(wave_app._monitor_task_died)
    with caplog.at_level(logging.CRITICAL, logger="wave"):
        with pytest.raises(ValueError):
            await task
        await asyncio.sleep(0)
    died = [r for r in caplog.records if "MONITOR LOOP DIED" in r.getMessage()]
    assert died and died[0].levelno == logging.CRITICAL
    assert "teardown blew up" in died[0].getMessage()


async def test_monitor_died_callback_silent_on_quit_cancellation(caplog):
    task = asyncio.get_running_loop().create_task(asyncio.sleep(60))
    task.add_done_callback(wave_app._monitor_task_died)
    with caplog.at_level(logging.CRITICAL, logger="wave"):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
    assert not any("MONITOR LOOP DIED" in r.getMessage() for r in caplog.records)
