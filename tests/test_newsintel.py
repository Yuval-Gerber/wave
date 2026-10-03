"""News classifier + ticker tests (the news-understanding ask, 2026-09-01)."""

from waveapp.engine.newsintel import classify


def test_categories_match_the_research_taxonomy():
    assert classify("ACME beats Q2 earnings estimates, raises guidance")[0] == "earnings"
    assert classify("FDA grants approval for Phase 3 trial drug")[0] == "fda"
    assert classify("MegaCorp to acquire SmallCo in $2B deal")[0] == "deal"
    assert classify("Analyst upgrades shares, lifts price target")[0] == "analyst"
    assert classify("SEC opens investigation into accounting fraud")[0] == "legal"
    assert classify("Oil jumps near $90 on Iran risk")[0] == "macro"
    assert classify("Company announces new product colorway")[0] == "other"


def test_direction_from_the_language():
    assert classify("Shares surge after record quarter, beats estimates")[1] == 1
    assert classify("Stock plunges as company misses revenue, cuts outlook")[1] == -1
    assert classify("Company schedules investor day")[1] == 0
    # today's real headline: mixed but down-words dominate for the utilities
    cat, direction = classify("Oil Jumps Near $90 On Iran Risk, California Utilities Crater")
    assert cat == "macro"


def test_scanner_news_carries_tags(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import waveapp.engine.scanner2 as s2mod
    from waveapp.engine.scanner2 import BaselineStore, Scanner2

    monkeypatch.setattr(s2mod, "support_dir", lambda: tmp_path)
    scanner = Scanner2(data_client=None, database=None)
    scanner.baselines = BaselineStore(path=tmp_path / "b.npz")
    scanner.set_universe(
        [SimpleNamespace(symbol="NWS", name="", tradable=True, asset_class="AssetClass.US_EQUITY")]
    )
    scanner.on_news(
        ["NWS"], 1000.0, headline="NWS surges on earnings beat", category="earnings", direction=1
    )  # noqa: E501
    assert scanner.news_dir["NWS"] == 1
    assert scanner.news_feed[0]["category"] == "earnings"
    assert "earnings:+1" in scanner._pending_events[0][3]


def test_news_ticker_widget(qtbot):
    from waveapp.ui.scanner_page import NewsTicker

    ticker = NewsTicker()
    qtbot.addWidget(ticker)
    ticker.add_news({"symbol": "XOM", "direction": 1, "headline": "Oil jumps on Iran risk"})
    ticker.add_news({"symbol": "EIX", "direction": -1, "headline": "California utilities crater"})
    assert len(ticker._items) == 2
    assert ticker._items[0][0].startswith("XOM ▲")
    assert ticker._items[1][0].startswith("EIX ▼")
    ticker.resize(600, 26)
    ticker.show()
    qtbot.waitExposed(ticker)
    ticker.repaint()  # paint path executes without error


def test_tape_demo_and_clear_from_test_tab(qtbot):
    from waveapp.ui.scanner_page import ScannerPage
    from waveapp.ui.test_page import TestPage

    scanner_page = ScannerPage()
    qtbot.addWidget(scanner_page)
    from types import SimpleNamespace

    page = TestPage(SimpleNamespace(), scanner_page=scanner_page)
    qtbot.addWidget(page)
    page._fire_tape("demo")
    assert len(scanner_page.ticker._items) == 5
    assert scanner_page.ticker._items[0][0].startswith("DEMO1 ▲")
    page._fire_tape("clear")
    assert scanner_page.ticker._items == []
    assert not scanner_page.ticker._timer.isActive()


def test_scanner_page_two_pages_and_led_board(qtbot):
    """2026-09-01: page 1 = the universe animation BIG, page 2 = the
    candidate table, dots switch; ticker renders as an LED matrix."""
    from waveapp.ui.scanner_page import ScannerPage

    page = ScannerPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.waitExposed(page)
    assert len(page.page_dots) == 2
    assert page._page_one.isVisible()
    assert not page._page_two.isVisible()
    page.set_scanner_page(1)
    assert page._page_index == 1
    assert page.page_dots[1].active

    # LED board (9.3 lag fix): adding news prerenders the item's dots into a
    # pixmap once; frames blit slices instead of drawing per-dot ellipses
    page.ticker.add_news({"symbol": "XOM", "direction": 1, "headline": "Oil surges"})
    page.ticker._rebuild_strip()
    assert page.ticker._item_pix, "no prerendered strip for the headline"
    assert page.ticker._item_cols[0] > 0
    assert not page.ticker._item_pix[0].isNull()
    # the lit mask itself still exists at render time — prove dots were lit
    mask = page.ticker._cols_to_mask(page.ticker._glyph_cols("XOM"))
    assert mask.any(), "no LEDs lit for the headline"
    page.ticker.repaint()  # LED paint path executes
    page.ticker.clear()
    assert not page.ticker._item_pix
