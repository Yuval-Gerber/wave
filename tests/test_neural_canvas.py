"""Phase 8.7: Wave's mind — the neural canvas and its scanner-event wiring."""

from waveapp.ui.neural_canvas import NODE_COUNT, NeuralCanvas
from waveapp.ui.scanner_page import ScannerPage


def _canvas(qtbot) -> NeuralCanvas:
    canvas = NeuralCanvas(seed=7)
    qtbot.addWidget(canvas)
    canvas.resize(400, 260)
    canvas.show()  # hidden canvases skip ALL work (the laggy-UI fix)
    canvas._tick()
    return canvas


def test_nodes_spawn_and_drift(qtbot):
    canvas = _canvas(qtbot)
    assert len(canvas._nodes) == NODE_COUNT  # every scanned stock is a dot
    before = [(n.px, n.py) for n in canvas._nodes]
    canvas._tick()
    after = [(n.px, n.py) for n in canvas._nodes]
    assert before != after  # the galaxy rotates — the brain is alive
    assert not canvas.grab().isNull()


def test_events_queue_and_play_spaced(qtbot):
    canvas = _canvas(qtbot)
    canvas.candidate_found("AAPL")
    canvas.candidate_rejected("TSLA", "spread too wide")
    canvas.candidate_promoted("NVDA")
    assert len(canvas._events) == 3  # queued — thoughts play one at a time
    canvas._play_next_event()
    assert len(canvas._events) == 2
    assert any(n.excite > 0.9 for n in canvas._nodes)  # the found node fired
    assert canvas._labels and canvas._labels[0].text == "AAPL"
    canvas._play_next_event()
    assert any("spread too wide" in label.text for label in canvas._labels)
    canvas._play_next_event()
    assert any("NVDA" in label.text for label in canvas._labels)
    # a promotion lights a whole region green
    assert sum(1 for n in canvas._nodes if n.excite > 0.5) >= 3


def test_excitation_propagates_and_decays(qtbot):
    canvas = _canvas(qtbot)
    canvas.ambient = False  # isolate the spark from ambient brain life
    canvas._pulses.clear()
    node = canvas._nodes[0]
    node.excite = 1.0
    excited_before = sum(1 for n in canvas._nodes if n.excite > 0.05)
    canvas._tick()
    excited_after = sum(1 for n in canvas._nodes if n.excite > 0.05)
    assert excited_after >= excited_before  # neighbors caught the spark
    for _ in range(200):
        canvas._tick()
    assert all(n.excite < 0.3 for n in canvas._nodes)  # …and it fades back


def test_scan_pulse_spawns_wave(qtbot):
    canvas = _canvas(qtbot)
    canvas.pulse_scan()
    canvas._play_next_event()
    assert canvas._waves
    radius = canvas._waves[0].radius
    canvas._tick()
    assert canvas._waves[0].radius > radius  # the thought expands
    assert not canvas.grab().isNull()


def test_scanner_page_feeds_the_mind(qtbot):
    page = ScannerPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    rows = [
        {
            "symbol": "AAPL",
            "score": 3.2,
            "strategy": "ORB",
            "rvol": 4.1,
            "gap_pct": 2.0,
            "atr_pct": 1.4,
            "spread": 0.01,
            "decision": "✓ accepted",
        },
        {
            "symbol": "XYZ",
            "score": 0.4,
            "strategy": "GAP",
            "rvol": 0.8,
            "gap_pct": 0.1,
            "atr_pct": 0.5,
            "spread": 0.35,
            "decision": "spread too wide",
        },
    ]
    page.update_candidates(rows)
    kinds = [event[0] for event in page.mind._events]
    assert kinds[0] == "scan"  # the cycle opens with a pulse
    assert "promoted" in kinds and "rejected" in kinds
    # virtualized radar model (2026-09-03): accepted AAPL under WORTH
    # WATCHING, rejected XYZ folded into the collapsed THE REST tier
    model = page.candidates_model
    assert [model.entry_kind(i) for i in range(model.rowCount())] == [
        "header",
        "row",
        "header",
        "toggle",
    ]
    assert model.row_dict(1)["symbol"] == "AAPL"
    assert "2 candidates, 1 gate-accepted" in page.status_label.text()


# -- round 2: traveling lights + candidate details ----------------------------


def test_pulses_travel_and_kindle_targets(qtbot):
    canvas = _canvas(qtbot)
    canvas._spawn_pulse()
    assert canvas._pulses
    pulse = canvas._pulses[0]
    target = pulse.b
    target.excite = 0.0
    for _ in range(60):
        canvas._tick()
        if pulse not in canvas._pulses:
            break
    assert pulse not in canvas._pulses  # arrived
    assert (
        target.excite > 0.0 or target.excite == 0.0 and any(p.b is target for p in canvas._pulses)
    )  # kindled (may have decayed a little since arrival)
    assert not canvas.grab().isNull()


def test_event_nodes_radiate_pulses(qtbot):
    canvas = _canvas(qtbot)
    canvas.candidate_promoted("NVDA")
    canvas._play_next_event()
    assert len(canvas._pulses) >= 1  # the promotion radiates traveling light


def test_table_has_no_selection_and_double_click_opens_details(qtbot):

    page = ScannerPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    rows = [
        {
            "symbol": "AAPL",
            "score": 3.2,
            "strategy": "ORB",
            "rvol": 4.1,
            "gap_pct": 2.0,
            "atr_pct": 1.4,
            "spread": 0.01,
            "decision": "expected move $0.007 < required $0.066 (3x costs $0.022)",
        }
    ]
    page.update_candidates(rows)
    from PyQt6.QtWidgets import QAbstractItemView

    assert page.table.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
    model = page.candidates_model
    # the lone rejected row lives in THE REST — collapsed behind the toggle
    assert [model.entry_kind(i) for i in range(model.rowCount())] == ["header", "toggle"]
    page._open_candidate(0, 0)  # section header: no details to open
    assert not hasattr(page, "_candidate_popup")
    model.toggle_rest()
    row_ix = next(i for i in range(model.rowCount()) if model.row_dict(i) is not None)
    page._open_candidate(row_ix, 0)
    popup = page._candidate_popup
    assert popup is not None
    texts = [label.text() for label in popup.card.findChildren(type(page.status_label))]
    assert any("AAPL" in t for t in texts)
    assert any("REJECTED" in t for t in texts)
    assert any("3x costs" in t for t in texts)  # the FULL decision text


# -- round 3: the biggest brain -----------------------------------------------


def test_spiral_galaxy_swirls_with_depth(qtbot):
    """v3 (2026-08-20): differential rotation — inner stars orbit faster —
    plus a real near/far depth spread from the tilted plane."""
    canvas = _canvas(qtbot)
    depths = [n.depth for n in canvas._nodes]
    assert max(depths) - min(depths) > 0.25  # tilted-plane depth spread
    inner = min(canvas._nodes, key=lambda s: s.radius)
    outer = max(canvas._nodes, key=lambda s: s.radius)
    assert inner.speed > outer.speed * 1.5  # the core visibly out-spins the rim
    star = canvas._nodes[0]
    before = (star.px, star.py)
    for _ in range(60):
        canvas._tick()
    assert (star.px, star.py) != before  # the galaxy is turning
    # gradient: core stars run hot/golden, rim stars run cool/blue
    assert inner.color.blue() < outer.color.blue()
    assert inner.color.red() > outer.color.red() or inner.color.green() > outer.color.green()


def test_symbol_maps_to_stable_node_and_states(qtbot):
    """v2: same stock → same neuron; color is STATE (rejected fades fast,
    accepted persists) — the all-red/all-green wash is impossible."""
    canvas = _canvas(qtbot)
    node_a = canvas._node_for("AAPL")
    assert canvas._node_for("AAPL") is node_a  # stable mapping
    canvas.candidate_rejected("AAPL", "spread")
    canvas._play_next_event()
    assert node_a.state == "rejected"
    canvas.candidate_promoted("NVDA")
    canvas._play_next_event()
    accepted = canvas._node_for("NVDA")
    assert accepted.state == "accepted"
    for _ in range(90):  # ~3 seconds
        canvas._tick()
    assert node_a.state == "idle"  # the rejection flicker is long gone
    assert accepted.state == "accepted"  # the accept still glows
    # brightness spread NEVER copies state colors to neighbors
    assert sum(1 for n in canvas._nodes if n.state != "idle") <= 2


def test_pulse_relay_chains(qtbot):
    canvas = _canvas(qtbot)
    canvas.ambient = False
    canvas._pulses.clear()
    canvas._spawn_pulse(hops=3)
    assert canvas._pulses and canvas._pulses[0].hops == 3
    # with ambient off, only relays can extend the chain — and hops are
    # finite, so the light train always dies out
    for _ in range(600):
        canvas._tick()
        if not canvas._pulses:
            break
    assert not canvas._pulses  # chain exhausted its hops


def test_spontaneous_ripples_appear(qtbot):
    canvas = _canvas(qtbot)
    canvas._ripple_countdown = 1
    canvas._tick()
    assert canvas._waves  # a regional ripple fired on its own
    ripple = canvas._waves[-1]
    assert ripple.growth < 7.5  # smaller/slower than the central scan wave


def test_scanner_build_progress_wave_and_ready(qtbot):
    """2026-08-20: wave animation + counting numbers while the scan
    set builds; 'ready ✓' when done; idle note passes through at night."""
    page = ScannerPage()
    qtbot.addWidget(page)
    page.set_build_progress(23, 65, "building today's scan set — batch 23/65")
    assert page.build_wave.isVisibleTo(page)
    assert "batch 23/65" in page.status_label.text()
    assert "4,600 symbols ranked" in page.status_label.text()
    page.set_build_progress(65, 65, "done")
    assert not page.build_wave.isVisibleTo(page)
    assert "ready ✓" in page.status_label.text()
    page.set_build_progress(0, 0, "overnight — scanning resumes 04:00 ET (exit-only session)")
    assert "overnight" in page.status_label.text()
    assert not page.build_wave.isVisibleTo(page)


def test_test_tab_scan_loader_demo_plays_and_clears(qtbot):
    """2026-08-21: Test-tab preview of the scan-set loader — play
    animates the wave + counter on the Scanner header, clear resets it."""
    from waveapp.ui.test_page import TestPage
    from waveapp.ui.top_bar import TopBar

    bar = TopBar()
    qtbot.addWidget(bar)
    page = ScannerPage()
    qtbot.addWidget(page)
    bench = TestPage(bar, scanner_page=page)
    qtbot.addWidget(bench)
    bench._play_scan_loader()
    assert page.build_wave.isVisibleTo(page)
    for _ in range(10):
        bench._loader_step()
    assert "batch 10/65" in page.status_label.text()
    bench._loader_done = 64
    bench._loader_step()
    assert "ready ✓" in page.status_label.text()
    assert not page.build_wave.isVisibleTo(page)
    page.set_status("last scan 09:31:00 — 1500 candidates, 7 gate-accepted")
    bench._play_scan_loader()
    bench._clear_scan_loader()
    assert not bench._loader_timer.isActive()
    # clear restores the last REAL status, not a stale placeholder
    assert "last scan 09:31:00" in page.status_label.text()


def test_build_wave_mirrors_to_flow_left_to_right(qtbot):
    """2026-08-21: the loader wave flows L→R like the login page.
    The raw stroke runs R→L, so mirrored mid-draw slices sit at LOW x."""
    from waveapp.ui.wave_shape import wave_points

    page = ScannerPage()
    qtbot.addWidget(page)
    assert page.build_wave.mirror is True
    # geometry proof: early-draw slice (0..0.3) of the raw R→L stroke sits at
    # HIGH x; the mirrored window (0.7..1.0) sits at LOW x — the left tip
    raw = [x for x, _ in wave_points(100, 30, start=0.0, end=0.3)]
    mirrored = [x for x, _ in wave_points(100, 30, start=0.7, end=1.0)]
    assert sum(mirrored) / len(mirrored) < sum(raw) / len(raw)


def test_week_card_counts_to_open_while_closed():
    """2026-08-22 (screenshot): during the weekend the card counted
    to 'the next closed minute' forever. Closed → next OPEN boundary."""
    from datetime import UTC, datetime

    from waveapp.engine.session import SessionScheduler

    saturday = datetime(2026, 8, 22, 15, 0, tzinfo=UTC)
    target = SessionScheduler.next_open_boundary(saturday)
    # the week opens Sunday 20:00 ET = Monday 00:00 UTC
    assert target.isoformat().startswith("2026-08-24T00:0")
    # mid-week: the next open after Tuesday RTH close is POST (same minute+1)
    tuesday = datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
    assert SessionScheduler.next_open_boundary(tuesday) > tuesday


# -- the radar table (2026-09-03: tiers, filters, spread %) --------------------


def _radar_rows() -> list[dict]:
    return [
        {
            "symbol": "AAPL",
            "score": 3.2,
            "strategy": "ORB",
            "rvol": 4.1,
            "gap_pct": 2.0,
            "atr_pct": 1.4,
            "spread": 0.02,
            "price": 230.0,
            "day_pct": 1.8,
            "decision": "✓ accepted",
        },
        {
            "symbol": "NVDA",
            "score": 4.5,
            "strategy": "GAP",
            "rvol": 6.0,
            "gap_pct": 5.0,
            "atr_pct": 3.1,
            "spread": 0.05,
            "price": 125.0,
            "day_pct": 4.2,
            "decision": "✓ accepted",
        },
        {
            "symbol": "RBLX",
            "score": 2.2,
            "strategy": "VWAP",
            "rvol": 3.0,
            "gap_pct": 1.0,
            "atr_pct": 2.0,
            "spread": 0.03,
            "price": 60.0,
            "day_pct": -0.5,
            "decision": "spread too wide",
        },
        {
            "symbol": "XYZ",
            "score": 0.4,
            "strategy": "GAP",
            "rvol": 0.8,
            "gap_pct": 0.1,
            "atr_pct": 0.5,
            "spread": 0.35,
            "price": 5.0,
            "day_pct": -2.0,
            "decision": "rvol too low",
        },
    ]


def _radar_page(qtbot):
    page = ScannerPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    return page


def test_radar_tier_ordering_and_menu_membership(qtbot):
    """Menu names lead (even a gate-rejected one — membership beats decision),
    accepted watchers follow, THE REST stays folded and unmaterialized."""
    from PyQt6.QtCore import Qt

    page = _radar_page(qtbot)
    page.set_menu(["RBLX"])
    page.update_candidates(_radar_rows())
    model = page.candidates_model
    kinds = [model.entry_kind(i) for i in range(model.rowCount())]
    assert kinds == ["header", "row", "header", "row", "row", "header", "toggle"]
    assert "ON THE MENU" in model.data(model.index(0, 0))
    assert model.row_dict(1)["symbol"] == "RBLX"
    # the menu tier carries the blue selection wash
    assert model.data(model.index(1, 0), Qt.ItemDataRole.BackgroundRole) is not None
    assert "WORTH WATCHING" in model.data(model.index(2, 0))
    assert {model.row_dict(3)["symbol"], model.row_dict(4)["symbol"]} == {"AAPL", "NVDA"}
    assert "THE REST" in model.data(model.index(5, 0))
    assert "Show all 1" in model.data(model.index(6, 0))
    # expanding materializes XYZ; toggling back folds it away again
    model.toggle_rest()
    assert any((model.row_dict(i) or {}).get("symbol") == "XYZ" for i in range(model.rowCount()))
    model.toggle_rest()
    assert all((model.row_dict(i) or {}).get("symbol") != "XYZ" for i in range(model.rowCount()))


def test_radar_search_and_chip_filters(qtbot):
    """The search box filters live and auto-opens THE REST (a hunt must never
    dead-end on a folded tier); chips filter by decision/menu membership."""
    page = _radar_page(qtbot)
    page.update_candidates(_radar_rows())
    model = page.candidates_model

    page.search_box.setText("xyz")
    symbols = [(model.row_dict(i) or {}).get("symbol") for i in range(model.rowCount())]
    assert "XYZ" in symbols  # found inside the (auto-opened) rest tier
    assert "AAPL" not in symbols
    page.search_box.setText("")

    page.chip_buttons["accepted"].click()
    symbols = {
        model.row_dict(i)["symbol"]
        for i in range(model.rowCount())
        if model.row_dict(i) is not None
    }
    assert symbols == {"AAPL", "NVDA"}
    page.chip_buttons["all"].click()
    assert model.rowCount() > 0


def test_radar_spread_pct_decision_glyphs_and_tooltip(qtbot):
    """Spread % = spread/price×100 at 2 decimals; ✓/✗ decision glyphs; gap,
    ATR and the reject reason live in the row tooltip, not in columns."""
    from PyQt6.QtCore import Qt

    page = _radar_page(qtbot)
    page.update_candidates(_radar_rows())
    model = page.candidates_model
    model.toggle_rest()

    def row_ix(symbol: str) -> int:
        return next(
            i for i in range(model.rowCount()) if (model.row_dict(i) or {}).get("symbol") == symbol
        )

    nvda = row_ix("NVDA")
    assert model.data(model.index(nvda, 5)) == "0.04%"  # 0.05 / 125 × 100
    assert model.data(model.index(nvda, 6)) == "✓"
    assert model.data(model.index(nvda, 1)).endswith("+4.20%")  # Price · Day %
    xyz = row_ix("XYZ")
    assert model.data(model.index(xyz, 6)) == "✗"
    tip = model.data(model.index(xyz, 0), Qt.ItemDataRole.ToolTipRole)
    assert "rvol too low" in tip and "Gap" in tip and "ATR" in tip


def test_radar_sort_stays_within_tiers(qtbot):
    """Header-click sorting reorders rows INSIDE each tier — the tier blocks
    themselves never interleave."""
    from PyQt6.QtCore import Qt

    page = _radar_page(qtbot)
    page.update_candidates(_radar_rows())
    model = page.candidates_model
    model.sort(3, Qt.SortOrder.DescendingOrder)  # Score, high first
    kinds = [model.entry_kind(i) for i in range(model.rowCount())]
    assert kinds == ["header", "row", "row", "header", "toggle"]
    assert model.row_dict(1)["symbol"] == "NVDA"  # 4.5 outranks AAPL's 3.2
    assert model.row_dict(2)["symbol"] == "AAPL"
    model.sort(-1)  # indicator cleared → back to the scanner's own order
    assert model.row_dict(1)["symbol"] == "AAPL"
