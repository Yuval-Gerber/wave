"""Broker-truth per-trade P&L — the two real 2026-09-15 cases."""

from waveapp.engine.pnl_truth import reconcile_symbol_day


def test_hand_completed_exit_credits_the_lineage():
    # TRMD morning: Wave's stop sold 379; a human sold the other 378 while
    # the old build's bug jammed the actor. The card showed +$413; broker
    # truth was +$829. All fills fall inside the lineage window -> full credit.
    fills = [
        {"side": "buy", "qty": 757, "price": 33.24, "t": 1000.0},
        {"side": "sell", "qty": 379, "price": 34.33, "t": 2000.0},  # Wave stop
        {"side": "sell", "qty": 378, "price": 34.34, "t": 2010.0},  # hand fill
    ]
    lineages = [{"uuid": "trmd1", "qty": 757, "opened": 990.0, "closed": 2020.0}]
    out = reconcile_symbol_day(fills, lineages)
    assert abs(out["trmd1"] - (379 * (34.33 - 33.24) + 378 * (34.34 - 33.24))) < 0.01


def test_split_lineage_allocates_by_capacity_fifo():
    # FPS: one 471-share buy feeding two lineages (330 banked + 141 adopted).
    fills = [
        {"side": "buy", "qty": 471, "price": 31.00, "t": 1000.0},
        {"side": "sell", "qty": 330, "price": 32.29, "t": 2000.0},
        {"side": "sell", "qty": 141, "price": 32.41, "t": 3000.0},
    ]
    lineages = [
        {"uuid": "fps330", "qty": 330, "opened": 990.0, "closed": 2005.0},
        {"uuid": "fps141", "qty": 141, "opened": 990.0, "closed": 3005.0},
    ]
    out = reconcile_symbol_day(fills, lineages)
    assert abs(out["fps330"] - 330 * (32.29 - 31.00)) < 0.01
    assert abs(out["fps141"] - 141 * (32.41 - 31.00)) < 0.01


def test_unmatched_fills_are_ignored():
    # A manual trade while Wave held nothing belongs to no card.
    fills = [
        {"side": "buy", "qty": 100, "price": 10.0, "t": 100.0},
        {"side": "sell", "qty": 100, "price": 11.0, "t": 200.0},
    ]
    out = reconcile_symbol_day(fills, [])
    assert out == {}


def test_idempotent_math_with_partial_fills():
    # Partial exit fills (broker splits one sell into pieces) still sum.
    fills = [
        {"side": "buy", "qty": 200, "price": 50.0, "t": 100.0},
        {"side": "sell", "qty": 120, "price": 51.0, "t": 200.0},
        {"side": "sell", "qty": 80, "price": 51.5, "t": 201.0},
    ]
    lineages = [{"uuid": "x", "qty": 200, "opened": 90.0, "closed": 210.0}]
    out = reconcile_symbol_day(fills, lineages)
    assert abs(out["x"] - (120 * 1.0 + 80 * 1.5)) < 0.01


# -- side-aware pairing (2026-09-25 loss-side study) ---------------------------
# The long-only matcher zeroed EVERY short of the week: BE's real -$501 and
# KGC's real +$258 both became 0.0 overnight (log 2026-09-25T00:05:12 UTC:
# "BE lineage e4f9800d: -500.99 -> +0.00"). A short's sell_short entry
# matched no long lot and its cover buy was misfiled as an unsold long
# entry. These tests pin the side-aware pairing.


def test_short_round_trip_keeps_the_loss():
    # BE 2026-09-24: shorted 33 @ 252.068485, stop covered @ 267.25.
    # The actor booked -500.99; broker-fill truth must agree, never 0.0.
    fills = [
        {"side": "sell_short", "qty": 33, "price": 252.068485, "t": 1000.0},
        {"side": "buy", "qty": 33, "price": 267.25, "t": 5000.0},
    ]
    lineages = [{"uuid": "be1", "side": "short", "qty": 33, "opened": 990.0, "closed": 5010.0}]
    out = reconcile_symbol_day(fills, lineages)
    assert abs(out["be1"] - 33 * (252.068485 - 267.25)) < 0.01  # ~= -500.99


def test_short_round_trip_keeps_the_win():
    # KGC 2026-09-24: shorted 487 @ 25.43, covered @ 24.90 -> +258.11.
    fills = [
        {"side": "sell_short", "qty": 487, "price": 25.43, "t": 1000.0},
        {"side": "buy", "qty": 487, "price": 24.90, "t": 2500.0},
    ]
    lineages = [{"uuid": "kgc1", "side": "short", "qty": 487, "opened": 990.0, "closed": 2510.0}]
    out = reconcile_symbol_day(fills, lineages)
    assert abs(out["kgc1"] - 258.11) < 0.01


def test_short_partial_cover_fills_sum():
    # Broker splits the cover into pieces; each credits the short lineage.
    fills = [
        {"side": "sell_short", "qty": 100, "price": 40.0, "t": 100.0},
        {"side": "buy", "qty": 60, "price": 39.0, "t": 200.0},
        {"side": "buy", "qty": 40, "price": 38.5, "t": 201.0},
    ]
    lineages = [{"uuid": "s", "side": "short", "qty": 100, "opened": 90.0, "closed": 210.0}]
    out = reconcile_symbol_day(fills, lineages)
    assert abs(out["s"] - (60 * 1.0 + 40 * 1.5)) < 0.01


def test_long_and_short_lineages_same_symbol_day():
    # A long round trip in the morning, a short round trip after — the
    # netted books must keep each trade's P&L on its own card.
    fills = [
        {"side": "buy", "qty": 50, "price": 20.0, "t": 100.0},
        {"side": "sell", "qty": 50, "price": 21.0, "t": 500.0},  # long exit +50
        {"side": "sell_short", "qty": 80, "price": 22.0, "t": 1000.0},
        {"side": "buy", "qty": 80, "price": 22.5, "t": 1500.0},  # short cover -40
    ]
    lineages = [
        {"uuid": "lg", "side": "long", "qty": 50, "opened": 90.0, "closed": 510.0},
        {"uuid": "sh", "side": "short", "qty": 80, "opened": 990.0, "closed": 1510.0},
    ]
    out = reconcile_symbol_day(fills, lineages)
    assert abs(out["lg"] - 50.0) < 0.01
    assert abs(out["sh"] - (-40.0)) < 0.01


def test_lineage_without_side_defaults_to_long():
    # Backward compatibility: pre-fix callers passed no "side".
    fills = [
        {"side": "buy", "qty": 10, "price": 5.0, "t": 100.0},
        {"side": "sell", "qty": 10, "price": 6.0, "t": 200.0},
    ]
    lineages = [{"uuid": "old", "qty": 10, "opened": 90.0, "closed": 210.0}]
    out = reconcile_symbol_day(fills, lineages)
    assert abs(out["old"] - 10.0) < 0.01


class _FakeActivityAdapter:
    def __init__(self, activities):
        self._activities = activities

    async def get_fill_activities(self, mode, after):
        return self._activities


def test_nightly_rewrite_preserves_and_repairs_short_rows(tmp_path):
    """The nightly broker-truth pass on a real schema DB:
    a short row the close path booked correctly stays put, a short row
    the old bug zeroed is repaired to broker truth, and a long row still
    reconciles. This is the exact BE/KGC 2026-09-24 shape."""
    import asyncio

    from waveapp.broker.base import TradingMode
    from waveapp.engine.pnl_truth import reconcile_day
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "p.db")
    day = "2026-09-24"
    rows = [
        # BE short: actor's close path wrote the CORRECT loss
        (
            "be1",
            "BE",
            "short",
            33,
            252.068485,
            -500.99,
            f"{day}T13:35:03+00:00",
            f"{day}T16:42:45+00:00",
        ),
        # KGC short: zeroed by the buggy pass (the state the live DB is in)
        ("kgc1", "KGC", "short", 487, 25.43, 0.0, f"{day}T13:35:03+00:00", f"{day}T13:59:36+00:00"),
        # AMD long control
        (
            "amd1",
            "AMD",
            "long",
            100,
            220.0,
            -100.0,
            f"{day}T14:00:00+00:00",
            f"{day}T15:00:00+00:00",
        ),
    ]
    for uuid, sym, side, qty, entry, pnl, opened, closed in rows:
        database.execute(
            "INSERT INTO positions (position_uuid, symbol, side, qty, avg_entry, state,"
            " strategy, opened_at, trading_mode, closed_at, realized_pnl) VALUES"
            " (?,?,?,?,?,'closed','GAP',?, 'paper',?,?)",
            (uuid, sym, side, qty, entry, opened, closed, pnl),
        )
    activities = [
        {
            "symbol": "BE",
            "side": "sell_short",
            "qty": 33,
            "price": 252.068485,
            "transaction_time": f"{day}T13:35:03Z",
        },
        {
            "symbol": "BE",
            "side": "buy",
            "qty": 33,
            "price": 267.25,
            "transaction_time": f"{day}T16:42:45Z",
        },
        {
            "symbol": "KGC",
            "side": "sell_short",
            "qty": 487,
            "price": 25.43,
            "transaction_time": f"{day}T13:35:03Z",
        },
        {
            "symbol": "KGC",
            "side": "buy",
            "qty": 487,
            "price": 24.90,
            "transaction_time": f"{day}T13:59:36Z",
        },
        {
            "symbol": "AMD",
            "side": "buy",
            "qty": 100,
            "price": 220.0,
            "transaction_time": f"{day}T14:00:00Z",
        },
        {
            "symbol": "AMD",
            "side": "sell",
            "qty": 100,
            "price": 219.0,
            "transaction_time": f"{day}T15:00:00Z",
        },
    ]
    adapter = _FakeActivityAdapter(activities)
    changed = asyncio.run(reconcile_day(adapter, TradingMode.PAPER, database, day))

    got = {
        r["position_uuid"]: float(r["realized_pnl"])
        for r in database.query("SELECT position_uuid, realized_pnl FROM positions")
    }
    assert abs(got["be1"] - (-500.99)) < 0.01  # never zeroed again
    assert abs(got["kgc1"] - 258.11) < 0.01  # repaired to broker truth
    assert abs(got["amd1"] - (-100.0)) < 0.01  # longs untouched-correct
    # only the zeroed short needed a rewrite
    assert [(s, round(new, 2)) for s, _, new in changed] == [("KGC", 258.11)]
    database.close()


def test_close_path_persists_a_shorts_negative_pnl(tmp_path):
    """The actor->positions close write (core.py _persist_position): a
    closed short actor's realized_pnl lands in the row signed as-is."""
    from waveapp.broker.base import OrderSide, TradingMode
    from waveapp.engine.actor import PositionActor, PositionSpec, PositionState
    from waveapp.engine.core import EngineCore
    from waveapp.persistence.db import Database

    database = Database(tmp_path / "c.db")
    spec = PositionSpec(symbol="BE", side=OrderSide.SELL, qty=33, stop_price=267.25)
    engine = EngineCore(object(), database=database)
    actor = PositionActor(spec, object(), TradingMode.PAPER)
    actor.filled_qty = 33.0
    actor.avg_entry_price = 252.068485
    engine._persist_position(actor)  # open row
    actor.state = PositionState.CLOSED
    actor.exit_reason = "stop hit"
    actor.realized_pnl = -500.99
    engine._persist_position(actor)  # close write
    row = database.query("SELECT side, realized_pnl, exit_path FROM positions")[0]
    assert row["side"] == "short"
    assert abs(float(row["realized_pnl"]) - (-500.99)) < 0.01
    assert row["exit_path"] == "stop hit"
    database.close()
