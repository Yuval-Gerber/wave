"""Phase 7 step 7.1: TradeGate math, heuristic ranker, journaling, scan flow."""

from datetime import UTC, datetime

from waveapp.broker.base import OrderSide
from waveapp.data.features import session_elapsed_fraction
from waveapp.engine.scanner import Scanner, SymbolFeatures, rank, strategy_scores
from waveapp.engine.session import ET
from waveapp.engine.tradegate import GateInputs, TradeGate
from waveapp.persistence.db import Database


def _features(**overrides) -> SymbolFeatures:
    base = dict(
        symbol="TEST",
        price=50.0,
        prev_close=50.0,
        gap_pct=0.0,
        rvol=1.0,
        atr_pct=1.5,
        spread=0.02,
        day_volume=1_000_000,
        avg_daily_volume=2_000_000,
    )
    base.update(overrides)
    return SymbolFeatures(**base)


def _gate_inputs(**overrides) -> GateInputs:
    base = dict(
        symbol="TEST",
        side=OrderSide.BUY,
        price=50.0,
        expected_move=0.20,
        profit_target=0.20,
        spread=0.02,
        slippage_buffer=0.01,
    )
    base.update(overrides)
    return GateInputs(**base)


# -- TradeGate ---------------------------------------------------------------


def test_gate_accepts_when_move_clears_costs():
    gate = TradeGate()
    decision = gate.evaluate(_gate_inputs())
    # costs ≈ 0.02 + 0.01 + fees(≈0.0013) ≈ 0.0313; required ≈ 0.094 < 0.20
    assert decision
    assert decision.required_move < 0.20


def test_gate_rejects_when_costs_dominate():
    gate = TradeGate()
    decision = gate.evaluate(_gate_inputs(expected_move=0.05))
    assert not decision
    assert "expected move" in decision.reason


def test_gate_spread_fraction_rule_and_exception():
    gate = TradeGate()
    # spread = 30% of target → over the 15% cap → rejected...
    decision = gate.evaluate(_gate_inputs(spread=0.06, expected_move=0.30))
    assert not decision and "spread" in decision.reason
    # ...unless the expected move is huge (wide-spread exception: ≥10× spread)
    decision = gate.evaluate(_gate_inputs(spread=0.06, expected_move=0.65))
    assert decision


def test_gate_charges_borrow_only_for_shorts():
    gate = TradeGate()
    long_decision = gate.evaluate(_gate_inputs(expected_move=0.11, borrow_fee=0.02))
    short_decision = gate.evaluate(
        _gate_inputs(
            side=OrderSide.SELL,
            expected_move=0.11,
            borrow_fee=0.02,
            shortable=True,  # S0: ETB proven, so the refusal gate passes and
            easy_to_borrow=True,  # the borrow CHARGE is what kills the trade
        )
    )
    assert long_decision  # borrow ignored for longs
    assert not short_decision  # 3×(costs+borrow) > 0.11
    assert "expected move" in short_decision.reason


def test_gate_refuses_non_etb_short_and_charges_modeled_borrow_for_etb():
    """S0 §8.4: a short candidate that hasn't proven shortable+ETB is refused
    outright (fail closed — also the GateInputs DEFAULT); an ETB short with
    no measured borrow_fee pays the modeled ETB line (paper shows no borrow
    fees; hard rule 10 says model them anyway)."""
    from waveapp.engine.tradegate import etb_borrow_fee_per_share

    gate = TradeGate()
    refused = gate.evaluate(_gate_inputs(side=OrderSide.SELL, expected_move=5.0))
    assert not refused
    assert "not shortable" in refused.reason
    htb = gate.evaluate(
        _gate_inputs(side=OrderSide.SELL, expected_move=5.0, shortable=True)
    )  # shortable but NOT easy-to-borrow → still refused
    assert not htb and "not shortable" in htb.reason
    etb = gate.evaluate(_gate_inputs(side=OrderSide.SELL, shortable=True, easy_to_borrow=True))
    long_twin = gate.evaluate(_gate_inputs())
    assert etb  # ETB short is tradable...
    modeled = etb_borrow_fee_per_share(50.0)
    assert modeled > 0
    assert abs((etb.total_cost - long_twin.total_cost) - modeled) < 1e-12  # ...but pays borrow


def test_gate_overnight_requires_eligibility():
    gate = TradeGate()
    decision = gate.evaluate(_gate_inputs(is_overnight=True, overnight_eligible=False))
    assert not decision and "overnight" in decision.reason
    assert gate.evaluate(_gate_inputs(is_overnight=True, overnight_eligible=True))


def test_gate_refuses_overnight_shorts_regardless_of_eligibility():
    """S1 (§6/§8.1): shorts NEVER enter overnight in v1 — even a fully
    ETB, overnight-eligible symbol is refused. (Overnight entries don't
    exist in code today; this is the backstop for when they do.)"""
    gate = TradeGate()
    decision = gate.evaluate(
        _gate_inputs(
            side=OrderSide.SELL,
            is_overnight=True,
            overnight_eligible=True,
            shortable=True,
            easy_to_borrow=True,
        )
    )
    assert not decision and "shorts never enter overnight" in decision.reason
    # the same inputs as a LONG still pass (unchanged behavior)
    assert gate.evaluate(_gate_inputs(is_overnight=True, overnight_eligible=True))


def test_gate_fees_come_from_the_database(tmp_path):
    database = Database(tmp_path / "t.db")
    gate = TradeGate(database=database)
    fees = gate.regulatory_fees_per_share(price=100.0, side=OrderSide.SELL)
    # SEC 20.60/1M × $100 + TAF 0.000195 ≈ 0.00206 + 0.000195
    assert abs(fees - (100 * 20.60 / 1_000_000 + 0.000195)) < 1e-9
    database.close()


# -- heuristic ranker --------------------------------------------------------


def test_gapper_with_volume_outranks_quiet_stock():
    hot = _features(symbol="HOT", gap_pct=5.0, rvol=4.0, atr_pct=3.0)
    quiet = _features(symbol="QUIET", gap_pct=0.1, rvol=0.8)
    ranked = rank([quiet, hot])
    assert ranked[0].features.symbol == "HOT"
    assert ranked[0].score > ranked[1].score
    assert ranked[0].best_strategy in ("GAP", "ORB")


def test_wide_spread_is_penalized():
    tight = strategy_scores(_features(rvol=3.0, spread=0.01))
    wide = strategy_scores(_features(rvol=3.0, spread=0.30))
    assert wide["ORB"] < tight["ORB"]


def test_illiquid_names_are_penalized():
    liquid = strategy_scores(_features(rvol=3.0))
    illiquid = strategy_scores(_features(rvol=3.0, avg_daily_volume=100_000))
    assert illiquid["VWAP"] < liquid["VWAP"]


# -- scan flow + journal -----------------------------------------------------


class FakeProvider:
    def __init__(self, features):
        self.features = features

    async def fetch(self):
        return self.features


async def test_scan_journals_every_symbol_and_gates_top_n(tmp_path):
    database = Database(tmp_path / "t.db")
    features = [
        _features(symbol="HOT", gap_pct=5.0, rvol=4.0, atr_pct=3.0),
        _features(symbol="MID", gap_pct=1.0, rvol=1.5),
        _features(symbol="DUD", gap_pct=0.0, rvol=0.2, spread=0.40),
    ]
    scanner = Scanner(
        provider=FakeProvider(features),
        gate=TradeGate(database=database),
        database=database,
        top_n=2,
    )
    results = await scanner.scan_once()

    assert results[0].features.symbol == "HOT"
    assert results[0].accepted  # big mover clears the gate
    assert results[-1].reject_reason == "below top-N cutoff"

    rows = database.query("SELECT * FROM candidates ORDER BY symbol")
    assert {r["symbol"] for r in rows} == {"HOT", "MID", "DUD"}  # ALL journaled
    hot_row = next(r for r in rows if r["symbol"] == "HOT")
    assert hot_row["decision"] == "accepted"
    assert '"rvol": 4.0' in hot_row["features"]
    database.close()


# -- session elapsed fraction ------------------------------------------------


def test_session_elapsed_fraction_clamps():
    def at(h, m):
        return datetime(2026, 8, 13, h, m, tzinfo=ET).astimezone(UTC)

    assert session_elapsed_fraction(at(9, 0)) == 0.05  # pre-open floor
    mid = session_elapsed_fraction(at(12, 45))  # half the session
    assert 0.45 < mid < 0.55
    assert session_elapsed_fraction(at(17, 0)) == 1.0


def test_log_redaction_filter():
    """Hard rule 6: no token-shaped string reaches any log destination."""
    import logging

    from waveapp.app import RedactSecretsFilter

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        "",
        0,
        "POST https://api.telegram.org/bot1234567890:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKK/getUpdates",
        (),
        None,
    )
    RedactSecretsFilter().filter(record)
    assert "bot<redacted>" in record.getMessage()
    assert "AAAbbb" not in record.getMessage()

    fake_key = "PK" + "ABCDEFGH1234567890"  # built at runtime: the literal
    record = logging.LogRecord(  # must not appear in this file (our own guard)
        "x", logging.INFO, "", 0, f"key {fake_key} leaked", (), None
    )
    RedactSecretsFilter().filter(record)
    assert "<redacted-key>" in record.getMessage()


def test_universe_cache_roundtrip(tmp_path, fake_keychain):
    """Same-day cache skips the ~6-min universe rebuild on relaunch."""
    import keyring

    keyring.set_password("Wave", "alpaca_paper_key_id", "test-key-id")
    keyring.set_password("Wave", "alpaca_paper_secret", "x" * 30)
    from waveapp.broker.base import AssetInfo
    from waveapp.data.features import AlpacaFeatureProvider

    assets = [AssetInfo("SPY", "SPDR", "ARCA", True, True, True, True)]
    provider = AlpacaFeatureProvider(assets, universe_size=150, cache_path=tmp_path / "cache.json")
    provider._scan_set = ["SPY", "AAPL"]
    provider._avg_volume = {"SPY": 1e6, "AAPL": 2e6}
    provider._daily_atr_pct = {"SPY": 1.1, "AAPL": 2.2}
    provider._save_cache()

    fresh = AlpacaFeatureProvider(assets, universe_size=150, cache_path=tmp_path / "cache.json")
    assert fresh._load_cache() == "fresh"
    assert fresh._scan_set == ["SPY", "AAPL"]
    assert fresh._daily_atr_pct["AAPL"] == 2.2

    # a stale (different-day) cache is refused
    import json

    data = json.loads((tmp_path / "cache.json").read_text())
    data["date"] = "2020-01-01"
    (tmp_path / "cache.json").write_text(json.dumps(data))
    assert fresh._load_cache() is None


def test_universe_day_key_is_et_after_utc_rollover(tmp_path, fake_keychain, monkeypatch):
    """A4-12 (audit 2026-09-22): between 20:00 ET and midnight ET the UTC
    date has rolled but the ET trading day has not — the UTC-keyed
    _universe_day forced one redundant cache reload every evening (and then
    pre-stamped tomorrow's day on yesterday's ranking). Both sides of the
    fetch() guard now speak the ET trading day."""
    import json
    from datetime import UTC, datetime

    import keyring

    keyring.set_password("Wave", "alpaca_paper_key_id", "test-key-id")
    keyring.set_password("Wave", "alpaca_paper_secret", "x" * 30)
    import waveapp.data.features as features_mod
    from waveapp.broker.base import AssetInfo
    from waveapp.data.features import AlpacaFeatureProvider

    class _EveningDT(datetime):
        """21:00 ET Sep 22 == 01:00 UTC Sep 23 — the mismatch window."""

        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 9, 23, 1, 0, tzinfo=UTC)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(features_mod, "datetime", _EveningDT)

    assets = [AssetInfo("SPY", "SPDR", "ARCA", True, True, True, True)]
    provider = AlpacaFeatureProvider(assets, universe_size=150, cache_path=tmp_path / "cache.json")
    provider._scan_set = ["SPY"]
    provider._avg_volume = {"SPY": 1e6}
    provider._daily_atr_pct = {"SPY": 1.1}
    provider._save_cache()

    # the cache is stamped with the ET trading day, not the rolled UTC date
    data = json.loads((tmp_path / "cache.json").read_text())
    assert data["date"] == "2026-09-22"

    fresh = AlpacaFeatureProvider(assets, universe_size=150, cache_path=tmp_path / "cache.json")
    assert fresh._load_cache() == "fresh"
    # the fetch() guard key: ET day on BOTH sides → no evening reload
    assert fresh._trading_day() == "2026-09-22"
    assert fresh._universe_day == fresh._trading_day()


def test_stale_cache_usable_while_revalidating(tmp_path, fake_keychain):
    """2026-08-20 ('5 min and still nothing'): yesterday's ranking
    scans IMMEDIATELY (allow_stale) while the rebuild runs in background."""
    import json

    import keyring

    keyring.set_password("Wave", "alpaca_paper_key_id", "test-key-id")
    keyring.set_password("Wave", "alpaca_paper_secret", "x" * 30)
    from waveapp.broker.base import AssetInfo
    from waveapp.data.features import AlpacaFeatureProvider

    assets = [AssetInfo("SPY", "SPDR", "ARCA", True, True, True, True)]
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "date": "2020-01-01",  # long stale
                "feed": "iex",
                "scan_set": ["SPY", "AAPL"],
                "avg_volume": {"SPY": 1e6},
                "atr_pct": {"SPY": 1.1},
            }
        )
    )
    provider = AlpacaFeatureProvider(assets, universe_size=150, cache_path=path)
    assert provider._load_cache() is None  # strict: stale rejected
    assert provider._load_cache(allow_stale=True) == "stale"
    assert provider._scan_set == ["SPY", "AAPL"]  # usable NOW


async def test_journal_writes_one_row_per_symbol_day(tmp_path):
    """2026-08-23 fix 1 (froze the 8GB Air): a row EVERY scan cycle grew
    the DB 752MB in a week. Now: one row per (symbol, day, strategy); a
    later gate-accept UPGRADES the row instead of adding another."""
    database = Database(tmp_path / "w.db")
    features = [
        _features(symbol="HOT", gap_pct=5.0, rvol=4.0, atr_pct=3.0),
        _features(symbol="MID", gap_pct=1.0, rvol=1.5),
        _features(symbol="DUD", gap_pct=0.0, rvol=0.2, spread=0.40),
    ]
    scanner = Scanner(
        provider=FakeProvider(features),
        gate=TradeGate(database=database),
        database=database,
        top_n=2,
    )
    await scanner.scan_once()
    await scanner.scan_once()
    await scanner.scan_once()  # three cycles...
    rows = database.query("SELECT symbol, COUNT(*) AS n FROM candidates GROUP BY symbol")
    assert rows and all(int(r["n"]) == 1 for r in rows)  # ...one row each
    # a rejected symbol later accepted flips its ROW, not a new one
    from waveapp.engine.scanner import Candidate

    target = next(c for c in scanner.last_results if not c.accepted)
    upgraded = Candidate(
        features=target.features,
        score=target.score,
        strategy_scores=target.strategy_scores,
        best_strategy=target.best_strategy,
        accepted=True,
        reject_reason="",
    )
    scanner._journal([upgraded])
    row = database.query(
        "SELECT COUNT(*) AS n, decision FROM candidates WHERE symbol=?",
        (target.features.symbol,),
    )[0]
    assert int(row["n"]) == 1 and row["decision"] == "accepted"
    database.close()


def test_stale_bar_guard_survives_post_holiday_opens(tmp_path, fake_keychain):
    """Audit 2026-09-22 A4-3/A3-4 (repeat of the 2026-09-21 Monday bug): at a
    9:28 Tuesday scan after a Monday holiday, Friday's daily bar (timestamped
    midnight ET) is ~4.4d old — the 4d guard nulled EVERY symbol → 0 features
    at the open. The guard is now 6d: post-holiday bars pass, a genuinely
    dead/recycled listing (the years-stale AT phantom class) still dies."""
    import keyring

    keyring.set_password("Wave", "alpaca_paper_key_id", "test-key-id")
    keyring.set_password("Wave", "alpaca_paper_secret", "x" * 30)
    from datetime import timedelta
    from types import SimpleNamespace

    from waveapp.broker.base import AssetInfo
    from waveapp.data.features import AlpacaFeatureProvider

    assets = [AssetInfo("SPY", "SPDR", "ARCA", True, True, True, True)]
    provider = AlpacaFeatureProvider(assets, universe_size=150, cache_path=tmp_path / "c.json")
    provider._avg_volume = {"SPY": 1e6}
    provider._daily_atr_pct = {"SPY": 1.1}

    def snap(daily_age_days: float, prev_age_days: float):
        now = datetime.now(UTC)
        return SimpleNamespace(
            daily_bar=SimpleNamespace(
                timestamp=now - timedelta(days=daily_age_days),
                open=100.0,
                close=101.0,
                volume=500_000,
            ),
            previous_daily_bar=SimpleNamespace(
                timestamp=now - timedelta(days=prev_age_days),
                close=99.0,
            ),
            latest_quote=SimpleNamespace(bid_price=100.9, ask_price=101.0),
            latest_trade=SimpleNamespace(price=101.0),
        )

    # Tuesday after a Monday holiday: Friday's bar ≈ 4.4d, prev (Thu) ≈ 5.4d
    assert provider._to_features("SPY", snap(4.4, 5.4), elapsed=0.05) is not None
    # worst realistic double-closure (Sandy-style): daily ≈ 5.4d, prev ≈ 6.4d
    assert provider._to_features("SPY", snap(5.4, 6.4), elapsed=0.05) is not None
    # a genuinely stale bar still gets nulled → no features
    assert provider._to_features("SPY", snap(10.0, 12.0), elapsed=0.05) is None


def test_symbol_features_carry_day_open():
    """Trend gate (2026-08-24): features expose today's official open."""
    f = _features(symbol="HOT")
    assert hasattr(f, "day_open")


async def test_journal_features_are_side_aware(tmp_path):
    """M0 (2026-09-23): every candidates row carries side (day-trend sign,
    journal only — the scanner stays a direction-free ranker), day_pct,
    shortable, etb and ssr_active in its features JSON."""
    import json

    database = Database(tmp_path / "w.db")
    features = [
        _features(symbol="UPP", price=51.0, prev_close=50.0, gap_pct=2.0, rvol=3.0),
        _features(
            symbol="DWN",
            price=48.0,
            prev_close=50.0,
            gap_pct=-4.0,
            rvol=3.0,
            shortable=True,
            easy_to_borrow=True,
        ),
    ]
    scanner = Scanner(
        provider=FakeProvider(features),
        gate=TradeGate(database=database),
        database=database,
        ssr_check=lambda symbol: symbol == "DWN",
    )
    await scanner.scan_once()
    rows = {
        r["symbol"]: json.loads(r["features"])
        for r in database.query("SELECT symbol, features FROM candidates")
    }
    assert rows["UPP"]["side"] == "long" and rows["UPP"]["day_pct"] > 0
    assert rows["UPP"]["ssr_active"] is False and rows["UPP"]["etb"] is None
    assert rows["DWN"]["side"] == "short" and rows["DWN"]["day_pct"] < 0
    assert rows["DWN"]["shortable"] is True and rows["DWN"]["etb"] is True
    assert rows["DWN"]["ssr_active"] is True
    database.close()


async def test_journal_without_ssr_check_writes_null(tmp_path):
    """No SSR probe wired (engine not up yet) → ssr_active journals as NULL,
    never a fabricated False."""
    import json

    database = Database(tmp_path / "w.db")
    scanner = Scanner(
        provider=FakeProvider([_features(symbol="AAA", rvol=3.0)]),
        gate=TradeGate(database=database),
        database=database,
    )
    await scanner.scan_once()
    row = json.loads(database.query("SELECT features FROM candidates")[0]["features"])
    assert row["ssr_active"] is None
    database.close()
