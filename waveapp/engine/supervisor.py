"""Task supervisor (WAVE 2 spec item 1, approved 2026-09-16).

"Wave never sleeps and never goes blind": every registered engine loop is
watched; a loop that CRASHES (task done unexpectedly) or, where it exposes
a heartbeat, HANGS (beat stale while its regimes are active) is cancelled
and recreated within a minute, loudly. Born from 2026-09-16 morning: the
Mac's overnight sleep killed the scan loop silently — zero scans
07:49→10:02, no pre-market watch, no PMOM, and nothing noticed until a
hand restart.

Resurrection counts are kept per loop (System tab material). A supervisor
cycle failure never kills the supervisor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("wave.engine.supervisor")

CHECK_SECONDS = 60.0


@dataclass
class _Watched:
    name: str
    task: asyncio.Task
    restart: Callable[[], asyncio.Task]  # returns the NEW running task
    beat: Callable[[], float | None] | None = None  # epoch of last heartbeat
    stale_after: float = 300.0
    active: Callable[[], bool] | None = None  # False = loop may legitimately nap
    resurrections: int = 0


class Supervisor:
    def __init__(self) -> None:
        self._watched: dict[str, _Watched] = {}
        self._task: asyncio.Task | None = None

    def register(
        self,
        name: str,
        task: asyncio.Task,
        restart: Callable[[], asyncio.Task],
        beat: Callable[[], float | None] | None = None,
        stale_after: float = 300.0,
        active: Callable[[], bool] | None = None,
    ) -> None:
        self._watched[name] = _Watched(
            name=name, task=task, restart=restart, beat=beat, stale_after=stale_after, active=active
        )

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def status(self) -> list[tuple[str, bool, int]]:
        """(name, healthy, resurrections) per loop — System tab feed."""
        return [(w.name, not w.task.done(), w.resurrections) for w in self._watched.values()]

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            for w in list(self._watched.values()):
                try:
                    await self._check(w)
                except Exception:
                    logger.exception("supervisor check failed for %s — next cycle", w.name)

    async def _check(self, w: _Watched) -> None:
        crashed = w.task.done()
        hung = False
        if not crashed and w.beat is not None and (w.active is None or w.active()):
            last = w.beat()
            hung = last is not None and (time.time() - last) > w.stale_after
        if not crashed and not hung:
            return
        if crashed:
            exc = None
            with contextlib.suppress(Exception):
                exc = w.task.exception()
            logger.critical("loop %s CRASHED (%r) — resurrecting", w.name, exc)
        else:
            logger.critical(
                "loop %s HUNG (beat stale > %.0fs) — cancelling and resurrecting",
                w.name,
                w.stale_after,
            )
            w.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await w.task
        w.resurrections += 1
        w.task = w.restart()
