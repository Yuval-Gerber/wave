"""Shadow kitchen — tap wiring, second-bar aggregation, adopt/process/finish,
and the iron rule: it can never touch a broker."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from waveapp.engine.master_key import MasterKeyParams
from waveapp.engine.shadow_kitchen import (
    SecondBarAggregator,
    ShadowKitchen,
    session_marks,
)

SEC = 1000


def make_actor(symbol="AAPL", state="open", qty=100.0, entry=100.0):
    return SimpleNamespace(
        spec=SimpleNamespace(symbol=symbol, side=SimpleNamespace(value="buy"), qty=qty),
        state=SimpleNamespace(value=state),
        filled_qty=qty,
        avg_entry_price=entry,
        entry_filled_at=datetime.now(tz=UTC),
        adopted_entry_time=None,
    )


def make_hub():
    return SimpleNamespace(
        trade_tap=None,
        bar_builder=SimpleNamespace(bars=lambda s: []),
        watch=None,
    )


def make_kitchen(tmp_path, hub, actors, mode="shadow", rebuy_cb=None, judge="off", **params_over):
    sk = ShadowKitchen(
        hub_getter=lambda: hub,
        actors_getter=lambda: actors,
        journal_dir=tmp_path,
        params=MasterKeyParams(**params_over),
        mode=mode,
        rebuy_cb=rebuy_cb,
    )
    # hermetic tests: never read the user's live judge_mode config — the
    # kitchen-only tests run with the judge off; judge-integration tests
    # pass judge="drive" explicitly (2026-09-20)
    sk._judge_mode = judge
    return sk


FIXED_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)  # 10:00 ET, mid-session
FIXED_MS = int(FIXED_NOW.timestamp() * 1000)


class FakeDriveActor:
    """Records every ExitDecision; exposes the fields the driver touches."""

    def __init__(self, symbol="AAPL", qty=100.0, entry=100.0, stop=95.0):
        self.spec = SimpleNamespace(
            symbol=symbol, side=SimpleNamespace(value="buy"), qty=qty, stop_price=stop
        )
        self.state = SimpleNamespace(value="open")
        self.filled_qty = qty
        self.avg_entry_price = entry
        self.entry_filled_at = FIXED_NOW
        self.adopted_entry_time = None
        self.exit_engine = SimpleNamespace(remaining_qty=qty, current_stop=stop, pending_stop=None)
        self.decisions = []

    async def apply_exit_decision(self, decision):
        self.decisions.append(decision)


class TestAggregator:
    def test_rolls_seconds(self):
        agg = SecondBarAggregator()
        agg.on_trade(10.0, 5, 1_000_000)
        agg.on_trade(10.5, 5, 1_000_500)  # same second
        agg.on_trade(11.0, 5, 1_001_000)  # next second closes the first
        bars = agg.drain()
        assert len(bars) == 1
        assert bars[0] == {"t": 1_000_000, "o": 10.0, "h": 10.5, "l": 10.0, "c": 10.5, "v": 10}

    def test_late_tick_folds_into_open_second(self):
        agg = SecondBarAggregator()
        agg.on_trade(10.0, 1, 2_000_000)
        agg.on_trade(9.0, 1, 1_999_000)  # late — folds, never crashes
        assert agg.drain() == []
        agg.on_trade(10.0, 1, 3_000_000)
        assert agg.drain()[0]["l"] == 9.0


class TestShadowKitchen:
    async def test_attaches_the_trade_tap(self, tmp_path):
        hub = make_hub()
        sk = make_kitchen(tmp_path, hub, {})
        await sk.tick()
        assert hub.trade_tap is sk._tap

    async def test_tap_heals_after_hub_swap(self, tmp_path):
        hub = make_hub()
        sk = make_kitchen(tmp_path, hub, {})
        await sk.tick()
        hub.trade_tap = None  # a reconnect replaced the tap
        await sk.tick()
        assert hub.trade_tap is sk._tap

    async def test_adopts_open_long_and_processes_seconds(self, tmp_path):
        hub = make_hub()
        actors = {"abc123": make_actor()}
        sk = make_kitchen(tmp_path, hub, actors)
        await sk.tick()
        assert "abc123" in sk.positions
        sp = sk.positions["abc123"]
        assert not sp.finished and sp.engine is not None
        # trade ticks arrive → seconds close → the brain sees them
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        sk.on_trade_tick("AAPL", 100.2, 10, t0 + SEC)
        sk.on_trade_tick("AAPL", 100.3, 10, t0 + 2 * SEC)
        await sk.tick()
        assert sp.engine._entered_once  # the shadow entered where Wave entered
        assert sp.last_t_ms > 0

    async def test_short_actor_adopts_judge_only(self, tmp_path, caplog):
        """S2 (2026-09-23): a SELL actor is adopted LIVE — no MK engine (the
        brain is long-only bookkeeping), NOT finished, judge-managed via the
        judge-only slice. The old A2-5 'no software manager' warning is
        retired."""
        import logging

        actor = make_actor()
        actor.spec.side = SimpleNamespace(value="sell")
        sk = make_kitchen(tmp_path, make_hub(), {"s1": actor}, judge="shadow")
        with caplog.at_level(logging.INFO, logger="wave.trade.shadow"):
            await sk.tick()
        sp = sk.positions["s1"]
        assert not sp.finished, "a short is a live judge-only position now"
        assert sp.engine is None and sp.vwap is None and sp.regime is None
        assert sp.vol60 is not None  # the judge's fuel gauge
        assert sp.flatten_ms == 0  # the core's session flatten owns the exit
        assert any("adopted judge-only" in r.message for r in caplog.records)
        assert not any("has no judge" in r.message for r in caplog.records)
        rows = [
            json.loads(x)
            for f in tmp_path.glob("shadow_kitchen_*.jsonl")
            for x in f.read_text().splitlines()
        ]
        assert any(r["event"] == "adopt_short" for r in rows)

    async def test_journal_rows_are_written(self, tmp_path):
        hub = make_hub()
        sk = make_kitchen(tmp_path, hub, {"j1": make_actor(symbol="MSFT")})
        await sk.tick()
        files = list(tmp_path.glob("shadow_kitchen_*.jsonl"))
        assert files, "adopt row must be journaled"
        rows = [json.loads(x) for x in files[0].read_text().splitlines()]
        assert rows[0]["event"] == "adopt"
        assert rows[0]["symbol"] == "MSFT"

    async def test_never_touches_a_broker(self, tmp_path):
        """The shadow holds no broker reference — the API makes orders
        impossible by construction. This guards the constructor signature."""
        import inspect

        from waveapp.engine import shadow_kitchen as mod

        sig = inspect.signature(ShadowKitchen.__init__)
        assert "broker" not in sig.parameters
        assert "adapter" not in sig.parameters
        source = inspect.getsource(mod)
        assert "submit_order" not in source
        assert "place_order" not in source

    async def test_run_loop_cancels_cleanly(self, tmp_path):
        sk = make_kitchen(tmp_path, make_hub(), {})
        task = asyncio.ensure_future(sk.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestDriveMode:
    """mode="drive": the brain runs the real kitchen (2026-09-09)."""

    async def _adopted(self, tmp_path, actor, **kw):
        actors = {"k1": actor}
        sk = make_kitchen(tmp_path, make_hub(), actors, mode="drive", **kw)
        sk._clock = lambda: FIXED_NOW  # deterministic session marks
        await sk.tick()
        return sk, sk.positions["k1"]

    async def test_wrong_stop_becomes_a_real_exit(self, tmp_path):
        actor = FakeDriveActor()
        sk, sp = await self._adopted(tmp_path, actor)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        sk.on_trade_tick("AAPL", 97.0, 10, t0 + 30 * SEC)
        sk.on_trade_tick("AAPL", 96.0, 10, t0 + 60 * SEC)
        await sk.tick()
        exits = [d for d in actor.decisions if d.action.value == "exit_now"]
        assert exits and exits[0].reason.startswith("MK wrong")

    async def test_protective_stop_only_ratchets_up(self, tmp_path):
        actor = FakeDriveActor(stop=95.0)
        sk, sp = await self._adopted(tmp_path, actor)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        sk.on_trade_tick("AAPL", 100.1, 10, t0 + 25 * SEC)
        sk.on_trade_tick("AAPL", 100.2, 10, t0 + 50 * SEC)
        sk.on_trade_tick("AAPL", 99.6, 10, t0 + 80 * SEC)  # closes the rising bars
        await sk.tick()
        amends = [d for d in actor.decisions if d.action.value == "amend_stop"]
        assert amends, "the resting stop must follow the brain's protect level"
        assert all(d.new_stop > 95.0 for d in amends)
        assert sp.last_stop_px > 95.0
        before = sp.last_stop_px  # a falling market must never widen the stop
        sk.on_trade_tick("AAPL", 99.5, 10, t0 + 110 * SEC)
        sk.on_trade_tick("AAPL", 99.4, 10, t0 + 140 * SEC)
        await sk.tick()
        assert sp.last_stop_px == before

    async def test_blocked_rebuy_rolls_the_ledger_back(self, tmp_path):
        async def refuse(*a):
            return False

        actor = FakeDriveActor()
        sk, sp = await self._adopted(tmp_path, actor, rebuy_cb=refuse, cooldown_s=0)
        sp.regime = SimpleNamespace(feed=lambda t, v: (True, True))  # proven trend
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        sk.on_trade_tick("AAPL", 97.0, 10, t0 + 30 * SEC)  # wrong-stop out
        sk.on_trade_tick("AAPL", 99.9, 10, t0 + 60 * SEC)  # breakout above exit
        sk.on_trade_tick("AAPL", 99.95, 10, t0 + 90 * SEC)
        await sk.tick()
        assert sp.engine.held == 0.0  # refused rebuy fully reversed
        assert not any(f.action.startswith("rebuy") for f in sp.engine.s.fills)

    async def test_accepted_rebuy_requests_rebind(self, tmp_path):
        calls = []

        async def accept(symbol, px, stop_px, tag, qty_cap):
            calls.append((symbol, round(px, 2), tag, qty_cap))
            return True

        actor = FakeDriveActor()
        sk, sp = await self._adopted(tmp_path, actor, rebuy_cb=accept, cooldown_s=0)
        sp.regime = SimpleNamespace(feed=lambda t, v: (True, True))
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        sk.on_trade_tick("AAPL", 97.0, 10, t0 + 30 * SEC)
        sk.on_trade_tick("AAPL", 99.9, 10, t0 + 60 * SEC)
        sk.on_trade_tick("AAPL", 99.95, 10, t0 + 90 * SEC)
        await sk.tick()
        assert calls and calls[0][0] == "AAPL" and calls[0][2] == "rebuyB"
        assert sp.pending_rebind
        # a fresh actor for the same symbol appears → lineage rebinds to it
        actors = sk._actors_getter()
        actors["k2"] = FakeDriveActor(stop=99.0)
        await sk.tick()
        assert sp.position_key == "k2" and not sp.pending_rebind

    async def test_rebind_resets_judge_state_and_act_latch(self, tmp_path):
        """A2-6 (audit 2026-09-22): the rebind keeps the same ShadowPosition,
        so the OLD leg's judge (its entry/peak) and a latched judge_acted used
        to survive onto the rebought leg — judged against a dead entry
        (instant giveback churn) or never acted on at all. The rebind must
        clear all three, and the re-seed must use the NEW actor's fill."""
        from waveapp.engine import position_judge as PJ

        actor = FakeDriveActor()
        sk, sp = await self._adopted(tmp_path, actor, judge="shadow")
        # stale lineage: judge state from the dead leg + a latched act
        sp.judge = PJ.JudgeState(
            entry_px=90.0, strategy_stop=88.0, atr_px=0.5, symbol="AAPL", peak_px=105.0
        )
        sp.judge_acted = True
        sp.judge_acted_t_ms = 123_456
        sp.pending_rebind = True  # a rebuy was accepted; awaiting the new actor
        actors = sk._actors_getter()
        actors["k2"] = FakeDriveActor(entry=105.0, stop=99.0)
        await sk.tick()
        assert sp.position_key == "k2" and not sp.pending_rebind
        assert sp.judge is None, "the dead leg's judge must not survive the rebind"
        assert not sp.judge_acted and sp.judge_acted_t_ms == 0
        # the next judged second re-seeds from the NEW actor's fill price,
        # not the MK engine's original entry_price (still 100.0)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 105.2, 10, t0)
        sk.on_trade_tick("AAPL", 105.3, 10, t0 + SEC)  # closes the t0 bar
        await sk.tick()
        assert sp.judge is not None
        assert sp.judge.entry_px == 105.0
        assert sp.judge.peak_px <= 105.3  # peak comes from the new leg's own tape

    async def test_external_flat_syncs_the_ledger(self, tmp_path):
        actor = FakeDriveActor()
        sk, sp = await self._adopted(tmp_path, actor)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        sk.on_trade_tick("AAPL", 100.1, 10, t0 + 2 * SEC)
        await sk.tick()
        assert sp.engine.held > 0
        actor.state = SimpleNamespace(value="closed")  # stop fired in reality
        await sk.tick()
        assert sp.engine.held == 0.0
        assert any(f.action.startswith("ext:") for f in sp.engine.s.fills)


class TestJudgeActReArm:
    """A2-2 (2026-09-22): a judge act whose close FAILS (the NVDG path —
    broker refuses the submit, actor re-protects back to OPEN) must not
    disarm the judge forever. The latch re-arms after JUDGE_ACT_GRACE_MS
    while the actor is still alive, and the judge acts again."""

    async def _judged(self, tmp_path):
        from waveapp.engine import position_judge as PJ

        actor = FakeDriveActor()
        actor.spec.manager = "judge"
        sk = make_kitchen(tmp_path, make_hub(), {"k1": actor}, mode="drive", judge="drive")
        sk._clock = lambda: FIXED_NOW
        sk._mins_to_flatten = lambda: None  # far from the close — no guard
        await sk.tick()
        sp = sk.positions["k1"]
        # a pre-latched BANK stance: the acting block fires on the next bar
        # (want=RIDE at +0.6A merely becomes a dwell candidate, never flips
        # within the test's 13s window — FLIP_DWELL_SECONDS is 20)
        t1 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sp.judge = PJ.JudgeState(
            entry_px=100.0, strategy_stop=95.0, atr_px=0.5, symbol="AAPL", stance=PJ.BANK
        )
        return sk, sp, actor, t1

    @staticmethod
    def _exits(actor):
        return [d for d in actor.decisions if d.action.value == "exit_now"]

    async def test_failed_close_rearms_after_grace_and_acts_again(self, tmp_path, caplog):
        import logging

        sk, sp, actor, t1 = await self._judged(tmp_path)
        sk.on_trade_tick("AAPL", 100.3, 10, t1)
        sk.on_trade_tick("AAPL", 100.3, 10, t1 + SEC)  # closes the t1 bar
        await sk.tick()
        await asyncio.sleep(0)  # let the fire-and-forget exit task run
        assert len(self._exits(actor)) == 1  # the act fired
        assert sp.judge_acted and sp.judge_acted_t_ms == t1
        # FakeDriveActor never leaves "open" — the exit did NOT stick.
        # Inside the grace window the latch holds: no double-fire.
        sk.on_trade_tick("AAPL", 100.3, 10, t1 + 5 * SEC)  # closes t1+1s (1s after act)
        await sk.tick()
        await asyncio.sleep(0)
        assert len(self._exits(actor)) == 1
        assert sp.judge_acted
        # past the grace: re-arm (with a WARNING) and act AGAIN
        with caplog.at_level(logging.WARNING, logger="wave.trade.shadow"):
            sk.on_trade_tick("AAPL", 100.3, 10, t1 + 12 * SEC)  # closes t1+5s (in grace)
            await sk.tick()
            sk.on_trade_tick("AAPL", 100.3, 10, t1 + 13 * SEC)  # closes t1+12s (past it)
            await sk.tick()
        await asyncio.sleep(0)
        assert len(self._exits(actor)) == 2, "the judge must retry a close that failed"
        assert sp.judge_acted and sp.judge_acted_t_ms == t1 + 12 * SEC  # latched anew
        assert any("did not stick" in r.message for r in caplog.records)

    async def test_judge_act_task_is_strong_refed_and_logs_failures(self, tmp_path, caplog):
        """A5-6 handoff (audit 2026-09-22): the judge's exit task was a bare
        weak-ref ensure_future (GC segfault class) — now strong-refed in
        _oneshot_tasks until done, with a done-callback that logs a death."""
        import logging

        sk, sp, actor, t1 = await self._judged(tmp_path)

        async def boom(decision):
            raise RuntimeError("submit exploded")

        actor.apply_exit_decision = boom
        with caplog.at_level(logging.ERROR, logger="wave.trade.shadow"):
            sk.on_trade_tick("AAPL", 100.3, 10, t1)
            sk.on_trade_tick("AAPL", 100.3, 10, t1 + SEC)
            await sk.tick()
            assert len(sk._oneshot_tasks) == 1, "in-flight exit must be strongly held"
            for _ in range(3):  # run the task and its done-callback
                await asyncio.sleep(0)
        assert not sk._oneshot_tasks, "finished task must be discarded"
        assert any("exit task died" in r.message for r in caplog.records)

    async def test_successful_close_never_rearms(self, tmp_path):
        sk, sp, actor, t1 = await self._judged(tmp_path)
        sk.on_trade_tick("AAPL", 100.3, 10, t1)
        sk.on_trade_tick("AAPL", 100.3, 10, t1 + SEC)
        await sk.tick()
        await asyncio.sleep(0)
        assert len(self._exits(actor)) == 1
        actor.state = SimpleNamespace(value="closing")  # the exit is working
        sk.on_trade_tick("AAPL", 100.3, 10, t1 + 12 * SEC)
        sk.on_trade_tick("AAPL", 100.3, 10, t1 + 13 * SEC)
        await sk.tick()
        await sk.tick()
        await asyncio.sleep(0)
        assert len(self._exits(actor)) == 1  # dead/closing actor: no re-fire
        assert sp.judge_acted  # and the latch stays down


class TestSpaxFrozenChip:
    """A5-5 (audit 2026-09-22), the SPAX stuck/laggy stance chip:
    (1) _finish must pop the stance (and a finished brain whose REAL actor
    is still alive keeps being judged — the judge is the sole manager);
    (2) a thin tape (no drained bars) still gets a judged stance from the
    hub's freshest print, at most once per wall second;
    (3) a bug inside the judge surfaces at exception level, rate-limited."""

    async def _adopted(self, tmp_path, hub=None, judge="shadow"):
        actor = FakeDriveActor()
        actor.spec.manager = "judge"
        sk = make_kitchen(tmp_path, hub or make_hub(), {"k1": actor}, mode="drive", judge=judge)
        sk._clock = lambda: FIXED_NOW
        sk._mins_to_flatten = lambda: None
        await sk.tick()
        return sk, sk.positions["k1"], actor

    async def test_finish_pops_the_judge_stance(self, tmp_path):
        sk, sp, actor = await self._adopted(tmp_path)
        sk.judge_stances = {sp.position_key: ("RIDE", "stale opinion")}
        sk._finish(sp)
        assert sp.finished
        assert sp.position_key not in sk.judge_stances, "a dead brain must not leave a frozen chip"

    async def test_finished_brain_with_live_actor_keeps_judging(self, tmp_path):
        """Leg-1 decision: finished (ledger flat) + actor alive = the judge
        keeps running via _process's judge-only slice — a live actor with no
        judge is a management gap."""
        sk, sp, actor = await self._adopted(tmp_path)
        sk._finish(sp)
        assert sp.finished and sp.position_key not in getattr(sk, "judge_stances", {})
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.4, 10, t0)
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + SEC)  # closes the t0 bar
        await sk.tick()
        assert sp.position_key in sk.judge_stances, "live actor must get fresh stances"
        assert sp.judge is not None and sp.judge.entry_px == 100.0
        # once reality is flat too, the ghost-pop still works
        actor.state = SimpleNamespace(value="closed")
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + 2 * SEC)
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + 3 * SEC)
        await sk.tick()
        assert sp.position_key not in sk.judge_stances

    async def test_thin_tape_judges_on_latest_hub_print(self, tmp_path):
        hub = make_hub()
        hub.latest_trade_px = {"AAPL": 101.5}
        hub.latest_trade_ts = {"AAPL": FIXED_NOW}
        sk, sp, actor = await self._adopted(tmp_path, hub=hub)
        # NO trade ticks ever reached the aggregator — the adopt tick alone
        # must already have judged once from the hub's freshest print
        assert sp.position_key in sk.judge_stances
        assert sp.judge is not None
        assert sp.judge.entry_px == 100.0
        assert sp.judge.peak_px == pytest.approx(101.5)  # judged AT the print
        # throttle: same wall second → no re-judge; +2s → exactly one more
        calls = []
        inner = sk._judge_second
        sk._judge_second = lambda *a, **k: (calls.append(a), inner(*a, **k))
        await sk.tick()
        assert calls == [], "at most one synthetic judge per wall second"
        from datetime import timedelta

        sk._clock = lambda: FIXED_NOW + timedelta(seconds=2)
        await sk.tick()
        assert len(calls) == 1
        assert calls[0][2] == FIXED_MS + 2000  # t_ms is the CURRENT clock

    async def test_judge_errors_surface_rate_limited(self, tmp_path, caplog, monkeypatch):
        import logging
        import time as _time

        from waveapp.engine.shadow_kitchen import JUDGE_ERR_LOG_SECONDS

        sk, sp, actor = await self._adopted(tmp_path)

        def boom(*a, **k):
            raise RuntimeError("judge blew up")

        monkeypatch.setattr("waveapp.engine.position_judge.judge_second", boom)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        with caplog.at_level(logging.DEBUG, logger="wave.trade.shadow"):
            sk._judge_second(sp, actor, t0, 100.3, 50.0)
            sk._judge_second(sp, actor, t0 + SEC, 100.3, 50.0)  # inside window
            errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
            assert len(errors) == 1, "one loud line per window, not per second"
            assert "judge second FAILED" in errors[0].message
            # window elapses → the next failure is loud again
            sk._judge_err_mono = _time.monotonic() - (JUDGE_ERR_LOG_SECONDS + 1)
            sk._judge_second(sp, actor, t0 + 2 * SEC, 100.3, 50.0)
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]
        assert len(errors) == 2

    async def test_closing_actor_keeps_an_annotated_chip(self, tmp_path):
        """A5-7 (audit 2026-09-22): a CLOSING/HALTED actor's card is still
        displayed — the chip freezes (last stance kept, state annotated)
        instead of popping; only a terminal actor pops it."""
        sk, sp, actor = await self._adopted(tmp_path)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.4, 10, t0)
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + SEC)
        await sk.tick()
        stance_before = sk.judge_stances[sp.position_key][0]
        actor.state = SimpleNamespace(value="closing")
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + 2 * SEC)
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + 3 * SEC)
        await sk.tick()
        stance, why = sk.judge_stances[sp.position_key]
        assert stance == stance_before and "(closing)" in why
        # a state change REPLACES the annotation, never stacks it
        actor.state = SimpleNamespace(value="halted")
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + 4 * SEC)
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + 5 * SEC)
        await sk.tick()
        stance, why = sk.judge_stances[sp.position_key]
        assert stance == stance_before and "(halted)" in why and "(closing)" not in why
        # terminal: the ghost-pop finally takes the chip
        actor.state = SimpleNamespace(value="closed")
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + 6 * SEC)
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + 7 * SEC)
        await sk.tick()
        assert sp.position_key not in sk.judge_stances

    async def test_finished_with_closing_actor_freezes_not_pops(self, tmp_path):
        """A5-7 x A5-5: the tick-branch pop (finished brain, actor not
        'alive') must also treat closing/halted as judged-but-frozen and
        pop only on terminal (closed/error)."""
        sk, sp, actor = await self._adopted(tmp_path)
        sk._finish(sp)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.4, 10, t0)
        sk.on_trade_tick("AAPL", 100.5, 10, t0 + SEC)
        await sk.tick()  # A5-5 judge-only slice restores a fresh stance
        assert sp.position_key in sk.judge_stances
        actor.state = SimpleNamespace(value="closing")
        await sk.tick()
        stance, why = sk.judge_stances[sp.position_key]
        assert "(closing)" in why
        actor.state = SimpleNamespace(value="error")
        await sk.tick()
        assert sp.position_key not in sk.judge_stances


def make_short_actor(symbol="SHRT", qty=50.0, entry=100.0, stop=104.0):
    """A SELL actor: buy-stop ABOVE entry, exit engine carrying the measured
    ATR (the judge's A source for engine-less shorts)."""
    a = FakeDriveActor(symbol=symbol, qty=qty, entry=entry, stop=stop)
    a.spec.side = SimpleNamespace(value="sell")
    a.exit_engine.atr = 1.0  # $1 at a $100 entry → A = 1%
    return a


class TestShortJudge:
    """S2 (2026-09-23): the SELL wiring — engine=None flows through the
    judge-only slice, the judge state is born side=-1 (normalized frame),
    the trough seeds the frame peak, and the acting block's READY-take
    comparison flips (px >= trigger)."""

    async def _adopted(self, tmp_path, actor=None, judge="shadow", mode="shadow"):
        actor = actor or make_short_actor()
        sk = make_kitchen(tmp_path, make_hub(), {"s1": actor}, mode=mode, judge=judge)
        sk._clock = lambda: FIXED_NOW
        sk._mins_to_flatten = lambda: None
        await sk.tick()
        return sk, sk.positions["s1"], actor

    async def test_sell_actor_gets_judged_stance_appears(self, tmp_path, caplog):
        """A winning short's stance chip appears — RIDE as the price falls —
        and the state announces 'short judge armed' at birth."""
        import logging

        sk, sp, actor = await self._adopted(tmp_path)
        t1 = FIXED_MS + 2000
        with caplog.at_level(logging.INFO, logger="wave.judge"):
            for i in range(82):
                sk.on_trade_tick("SHRT", 100.0 - i * 0.05, 10, t1 + i * SEC)
            await sk.tick()
        assert sp.position_key in sk.judge_stances
        stance, why = sk.judge_stances[sp.position_key]
        assert stance == "RIDE", f"winning short must ride ({stance}: {why})"
        assert sp.judge is not None and sp.judge.side == -1
        assert sp.judge.entry_px == 100.0
        assert sp.judge.strategy_stop == 104.0  # REAL space — the frame transforms
        armed = [r for r in caplog.records if "short judge armed" in r.message]
        assert len(armed) == 1

    async def test_short_atr_sourced_from_actor_exit_engine(self, tmp_path):
        sk, sp, actor = await self._adopted(tmp_path)
        assert sk._atr_pct_for(sp, actor) == pytest.approx(1.0)  # atr $1 / entry $100
        actor.exit_engine.atr = 0.0  # no measurement → the 0.5% fallback
        assert sk._atr_pct_for(sp, actor) == pytest.approx(0.5)

    async def test_seed_trough_ratchets_the_frame_peak(self, tmp_path):
        """The monitor's mirror seed (judge_seed_trough — the LOW since
        entry) lands as the FRAME peak: 2*entry − trough, with its time and
        volume; an absent seed (0.0) must not fake a 2*entry peak."""
        actor = make_short_actor()
        actor.judge_seed_trough = 96.0
        actor.judge_seed_trough_t_ms = 5000
        actor.judge_seed_trough_vol = 777.0
        sk, sp, _ = await self._adopted(tmp_path, actor=actor)
        t1 = FIXED_MS + 2000
        sk.on_trade_tick("SHRT", 99.0, 10, t1)
        sk.on_trade_tick("SHRT", 99.0, 10, t1 + SEC)  # closes the t1 bar
        await sk.tick()
        assert sp.judge.peak_px == pytest.approx(104.0)  # 2*100 − 96
        assert sp.judge.peak_t_ms == 5000
        assert sp.judge.peak_vol60 >= 777.0
        # a long seeded 0.0 (no seed) stays unpoisoned — regression guard
        actor2 = make_short_actor(symbol="SH2")
        sk2 = make_kitchen(tmp_path, make_hub(), {"s2": actor2}, judge="shadow")
        sk2._clock = lambda: FIXED_NOW
        sk2._mins_to_flatten = lambda: None
        await sk2.tick()
        sk2.on_trade_tick("SH2", 99.0, 10, t1)
        sk2.on_trade_tick("SH2", 99.0, 10, t1 + SEC)
        await sk2.tick()
        sp2 = sk2.positions["s2"]
        assert sp2.judge.peak_px == pytest.approx(101.0)  # frame of 99.0, not 200

    async def test_ready_take_fires_on_rebound_through_trigger(self, tmp_path):
        """The flipped comparison: a short's READY take fires when the REAL
        price rebounds UP through trough + 0.35A (px >= trigger) — and not
        while the price sits below it."""
        from waveapp.engine import position_judge as PJ

        actor = make_short_actor()
        sk, sp, _ = await self._adopted(tmp_path, actor=actor, judge="drive", mode="drive")
        t1 = FIXED_MS + 2000
        st = PJ.JudgeState(
            entry_px=100.0,
            strategy_stop=104.0,
            atr_px=1.0,
            symbol="SHRT",
            side=-1,
            stance=PJ.READY,
            stance_since_ms=t1,
        )
        st.peak_px = st._to_frame(97.0)  # trough 97 → frame peak 103
        st.peak_t_ms, st.peak_vol60 = t1, 1000.0
        sp.judge = st
        exits = [d for d in actor.decisions if d.action.value == "exit_now"]
        assert exits == []
        # below the trigger: a new LOW (96.50 → trigger becomes 96.85) — the
        # win is growing, no take
        sk.on_trade_tick("SHRT", 96.50, 10, t1)
        sk.on_trade_tick("SHRT", 96.50, 10, t1 + SEC)
        await sk.tick()
        await asyncio.sleep(0)
        assert [d for d in actor.decisions if d.action.value == "exit_now"] == []
        assert not sp.judge_acted
        # the rebound: 97.50 >= 96.85 crosses the trigger — the take fires
        sk.on_trade_tick("SHRT", 97.50, 10, t1 + 2 * SEC)
        sk.on_trade_tick("SHRT", 97.50, 10, t1 + 3 * SEC)
        await sk.tick()
        await sk.tick()
        await asyncio.sleep(0)
        exits = [d for d in actor.decisions if d.action.value == "exit_now"]
        assert len(exits) == 1, "the short READY take must fire on the rebound"
        assert exits[0].reason == "judge READY take"
        assert sp.judge_acted

    async def test_terminal_short_finishes_and_pops_the_chip(self, tmp_path):
        """engine=None end-of-life: reality flat → the position retires
        through _finish (no MK ledger to score) and the chip pops."""
        sk, sp, actor = await self._adopted(tmp_path)
        t1 = FIXED_MS + 2000
        sk.on_trade_tick("SHRT", 99.5, 10, t1)
        sk.on_trade_tick("SHRT", 99.5, 10, t1 + SEC)
        await sk.tick()
        assert sp.position_key in sk.judge_stances
        actor.state = SimpleNamespace(value="closed")  # covered / stop fired
        await sk.tick()
        assert sp.finished
        assert sp.position_key not in sk.judge_stances
        rows = [
            json.loads(x)
            for f in tmp_path.glob("shadow_kitchen_*.jsonl")
            for x in f.read_text().splitlines()
        ]
        done = [r for r in rows if r["event"] == "done"]
        assert done and "shadow_pnl" not in done[0]  # no MK ledger on a short

    async def test_closing_short_keeps_frozen_chip(self, tmp_path):
        """A5-7 applies to shorts too: a CLOSING actor's chip freezes with
        the annotation instead of popping."""
        sk, sp, actor = await self._adopted(tmp_path)
        t1 = FIXED_MS + 2000
        sk.on_trade_tick("SHRT", 99.5, 10, t1)
        sk.on_trade_tick("SHRT", 99.5, 10, t1 + SEC)
        await sk.tick()
        stance_before = sk.judge_stances[sp.position_key][0]
        actor.state = SimpleNamespace(value="closing")
        sk.on_trade_tick("SHRT", 99.5, 10, t1 + 2 * SEC)
        sk.on_trade_tick("SHRT", 99.5, 10, t1 + 3 * SEC)
        await sk.tick()
        stance, why = sk.judge_stances[sp.position_key]
        assert stance == stance_before and "(closing)" in why


class TestM0Telemetry:
    """M0 (ML master plan, 2026-09-23): every stance flip and act lands in
    judge_transitions through the monitor's db closure — with dwell timing
    and forward outcomes. Observation only; no cb = no behavior change."""

    @staticmethod
    def _recording_cb():
        calls: list[tuple[str, dict]] = []

        def cb(op, payload):
            calls.append((op, dict(payload)))
            if op == "transition":
                return sum(1 for o, _ in calls if o == "transition")  # 1-based ids
            return None

        return calls, cb

    async def _adopted(self, tmp_path, actor=None, judge="shadow", mode="shadow", key="k1"):
        actor = actor or FakeDriveActor()
        sk = make_kitchen(tmp_path, make_hub(), {key: actor}, mode=mode, judge=judge)
        calls, cb = self._recording_cb()
        sk._journal_db_cb = cb
        sk._clock = lambda: FIXED_NOW
        sk._mins_to_flatten = lambda: None
        await sk.tick()
        return sk, sk.positions[key], actor, calls

    async def test_flip_writes_transition_row_with_dwell(self, tmp_path):
        sk, sp, actor, calls = await self._adopted(tmp_path)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        for i in range(1, 24):  # +0.8A holds through the 20s dwell → RIDE flip
            sk.on_trade_tick("AAPL", 100.4, 10, t0 + i * SEC)
        await sk.tick()
        flips = [p for op, p in calls if op == "transition"]
        assert len(flips) == 1, "one flip = one row"
        row = flips[0]
        assert row["from_stance"] == "WAIT" and row["to_stance"] == "RIDE"
        assert row["side"] == "long" and row["symbol"] == "AAPL"
        assert row["position_key"] == "k1"
        from waveapp.engine.position_judge import FLIP_DWELL_SECONDS

        assert row["dwell_ms"] >= FLIP_DWELL_SECONDS * 1000  # dwell honored
        assert row["entry_px"] == 100.0 and row["px"] == pytest.approx(100.4)
        assert row["profit_atr"] > 0.5 and row["peak_profit_atr"] >= row["profit_atr"]
        assert row["close_guard"] == 0 and row["failed_highs"] == 0
        assert row["reason"]

    async def test_forward_outcomes_fill_after_deadlines(self, tmp_path):
        sk, sp, actor, calls = await self._adopted(tmp_path)
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        for i in range(1, 24):
            sk.on_trade_tick("AAPL", 100.4, 10, t0 + i * SEC)
        await sk.tick()  # flip journaled at t0+21s
        assert sk._ml_pending, "a flip must arm a forward-outcome watch"
        # 36s past the flip: px_30s fills from the live tape; 2m still pending
        for i in range(24, 59):
            sk.on_trade_tick("AAPL", 100.5, 10, t0 + i * SEC)
        await sk.tick()
        ops = [op for op, _ in calls]
        assert "px30" in ops and "px2m" not in ops
        px30 = next(p for op, p in calls if op == "px30")
        assert px30["v"] == pytest.approx(100.5)
        # 2m past the flip: px_2m + frame-space MFE/MAE in ATR units
        for i in range(59, 166):
            sk.on_trade_tick("AAPL", 100.5, 10, t0 + i * SEC)
        await sk.tick()
        px2m = next(p for op, p in calls if op == "px2m")
        assert px2m["v"] == pytest.approx(100.5)
        assert px2m["mfe"] >= 0.0 and px2m["mae"] <= px2m["mfe"]
        assert "px5m" not in [op for op, _ in calls]  # 5m deadline not reached

    async def test_act_row_carries_flip_to_act_dwell(self, tmp_path):
        from waveapp.engine import position_judge as PJ

        actor = FakeDriveActor()
        actor.spec.manager = "judge"
        sk, sp, actor, calls = await self._adopted(
            tmp_path, actor=actor, judge="drive", mode="drive"
        )
        t1 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sp.judge = PJ.JudgeState(
            entry_px=100.0,
            strategy_stop=95.0,
            atr_px=0.5,
            symbol="AAPL",
            stance=PJ.BANK,
            stance_since_ms=t1 - 7_000,  # the BANK flip happened 7s ago
        )
        sk.on_trade_tick("AAPL", 100.3, 10, t1)
        sk.on_trade_tick("AAPL", 100.3, 10, t1 + SEC)  # closes the t1 bar
        await sk.tick()
        await asyncio.sleep(0)
        acts = [p for op, p in calls if op == "transition" and p["to_stance"].startswith("ACT:")]
        assert len(acts) == 1
        assert acts[0]["to_stance"].startswith("ACT:judge BANK")
        assert acts[0]["from_stance"] == "BANK"
        assert acts[0]["dwell_ms"] == 7_000  # flip→act

    async def test_short_flip_row_is_frame_space(self, tmp_path):
        """A falling tape is a WINNING short: profit_atr positive (frame
        space) while px stays the REAL price — shorts compare like longs."""
        sk, sp, actor, calls = await self._adopted(tmp_path, actor=make_short_actor(), key="s1")
        t1 = FIXED_MS + 2000
        for i in range(40):
            sk.on_trade_tick("SHRT", 100.0 - i * 0.05, 10, t1 + i * SEC)
        await sk.tick()
        flips = [p for op, p in calls if op == "transition"]
        assert flips, "the winning short must flip WAIT → RIDE"
        row = flips[-1]
        assert row["side"] == "short" and row["to_stance"] == "RIDE"
        assert row["profit_atr"] > 0, "frame space: a falling tape = profit"
        assert row["px"] < 100.0, "px stays the REAL print"
        assert row["entry_px"] == 100.0

    async def test_no_cb_means_no_telemetry_and_no_wound(self, tmp_path):
        """Default construction (no journal_db_cb): flips still happen,
        nothing is journaled to a DB, nothing raises."""
        sk = make_kitchen(tmp_path, make_hub(), {"k1": FakeDriveActor()}, judge="shadow")
        sk._clock = lambda: FIXED_NOW
        sk._mins_to_flatten = lambda: None
        await sk.tick()
        sp = sk.positions["k1"]
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        for i in range(1, 24):
            sk.on_trade_tick("AAPL", 100.4, 10, t0 + i * SEC)
        await sk.tick()
        assert sk.judge_stances[sp.position_key][0] == "RIDE"
        assert sk._ml_pending == []

    async def test_db_cb_failure_never_wounds_the_judge(self, tmp_path):
        def exploding_cb(op, payload):
            raise RuntimeError("db is on fire")

        sk = make_kitchen(tmp_path, make_hub(), {"k1": FakeDriveActor()}, judge="shadow")
        sk._journal_db_cb = exploding_cb
        sk._clock = lambda: FIXED_NOW
        sk._mins_to_flatten = lambda: None
        await sk.tick()
        sp = sk.positions["k1"]
        t0 = ((sp.engine.opened_ms // SEC) + 2) * SEC
        sk.on_trade_tick("AAPL", 100.0, 10, t0)
        for i in range(1, 24):
            sk.on_trade_tick("AAPL", 100.4, 10, t0 + i * SEC)
        await sk.tick()
        assert sk.judge_stances[sp.position_key][0] == "RIDE"  # judging survived


def test_mins_to_flatten_uses_injected_clock_and_exit_params_lead(tmp_path):
    """A2-7 (audit 2026-09-22): the closing-guard clock read datetime.now(UTC)
    instead of the kitchen's injected clock (replay/referee runs diverged onto
    the wall clock) and hardcoded the 10-minute lead. It now follows
    self._clock and ExitParams.flatten_before_close_minutes."""
    from waveapp.engine.exits import ExitParams

    lead = float(ExitParams.flatten_before_close_minutes)
    late = datetime(2026, 9, 22, 19, 45, tzinfo=UTC)  # Tue 15:45 ET — 15m to close
    sk = make_kitchen(tmp_path, make_hub(), {}, judge="off")
    sk._clock = lambda: late
    assert sk._mins_to_flatten() == pytest.approx(15.0 - lead)
    # the guard flips with the INJECTED clock, whatever the wall clock says
    sk._clock = lambda: datetime(2026, 9, 22, 14, 0, tzinfo=UTC)  # 10:00 ET
    sk._flatten_clock_cache = None  # drop the 5s cache between the two reads
    mins = sk._mins_to_flatten()
    assert mins is not None and mins > 100  # far from the close — no guard


async def test_post_flatten_adoption_disables_kitchen_flatten(tmp_path, caplog):
    """A2-8 (audit 2026-09-22): a position adopted AFTER 15:50 ET (POST) got
    flatten_ms in the past and the kitchen ledger flattened it (in drive:
    EXIT_NOW) on its very FIRST bar. The kitchen mirror is now disabled for
    such positions with a WARNING — the core's early-close-aware session
    flatten owns the exit."""
    import logging

    late = datetime(2026, 9, 22, 21, 30, tzinfo=UTC)  # 17:30 ET — POST
    actor = FakeDriveActor()
    actor.entry_filled_at = late
    sk = make_kitchen(tmp_path, make_hub(), {"k1": actor}, judge="off")
    sk._clock = lambda: late
    with caplog.at_level(logging.WARNING, logger="wave.trade.shadow"):
        await sk.tick()
    sp = sk.positions["k1"]
    assert sp.flatten_ms == 0  # disabled, not a past-time landmine
    assert any("flatten disabled" in r.message for r in caplog.records)
    t0 = int(late.timestamp() * 1000)
    sk.on_trade_tick("AAPL", 100.3, 10, t0)
    sk.on_trade_tick("AAPL", 100.3, 10, t0 + SEC)  # closes the first bar
    await sk.tick()
    assert not sp.finished
    assert sp.engine.held > 0, "the first bar must not flatten a post-flatten adoption"


def test_mk_delegated_params_disable_the_classic_layers():
    from waveapp.engine.exits import Regime, mk_delegated_params_for

    p = mk_delegated_params_for(Regime.MIDDAY)
    assert p.k_trail >= 999.0
    assert p.k_be >= 999.0
    assert p.t_max_minutes >= 9999
    assert p.vwap_recross_exit is False


def test_session_marks_are_et_anchored():
    now = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)  # 11:00 ET
    rth_open, last_reentry, flatten = session_marks(now)
    assert flatten - last_reentry == 20 * 60 * 1000
    assert (last_reentry - rth_open) == 6 * 3600 * 1000
