"""The wave line is a traced asset + pure math — test it headless."""

from waveapp.ui.wave_logo import loop_segment
from waveapp.ui.wave_shape import stroke_aspect, wave_points


def test_stroke_asset_loads():
    assert stroke_aspect() > 3  # the traced wave is a wide band
    pts = wave_points(100, 50)
    assert len(pts) > 100


def test_shape_is_continuous():
    pts = wave_points(100, 50)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
        assert abs(x2 - x1) < 8 and abs(y2 - y1) < 8


def test_draw_order_starts_at_right_tip():
    """The video draws from the right end — index 0 must be the right tip."""
    pts = wave_points(100, 50)
    assert pts[0][0] > 95
    assert pts[-1][0] < 5


def test_curl_doubles_back():
    """The curl region visits the same x at multiple heights."""
    pts = wave_points(100, 50)
    xs = [x for x, _ in pts]
    # x is NOT monotonic along the line (the curl wraps back)
    increases = sum(1 for a, b in zip(xs, xs[1:], strict=False) if b > a)
    decreases = sum(1 for a, b in zip(xs, xs[1:], strict=False) if b < a)
    assert increases > 10 and decreases > 10


def test_slicing_for_self_draw():
    full = wave_points(100, 50)
    half = wave_points(100, 50, start=0.0, end=0.5)
    tail_half = wave_points(100, 50, start=0.5, end=1.0)
    assert 0 < len(half) < len(full)
    # slices start/end where they should
    assert half[0] == full[0]
    assert tail_half[-1] == full[-1]
    # empty slice
    assert wave_points(100, 50, start=0.5, end=0.5) == []


def test_flatten_produces_flat_line():
    pts = wave_points(100, 50, flatten=0.0)
    ys = [y for _, y in pts]
    assert max(ys) - min(ys) < 1e-6


def test_jagged_displaces_points():
    smooth = wave_points(100, 50)
    jagged = wave_points(100, 50, jagged=1.0)
    diffs = [abs(a[1] - b[1]) for a, b in zip(smooth, jagged, strict=True)]
    assert max(diffs) > 0.5


def test_loop_segment_timeline():
    """Draw → hold → erase → rest, like the reference video."""
    s, e = loop_segment(0.2)  # drawing
    assert s == 0.0 and 0 < e < 1
    assert loop_segment(0.5) == (0.0, 1.0)  # hold: full wave
    s, e = loop_segment(0.8)  # erasing from the start side
    assert 0 < s < 1 and e == 1.0
    s, e = loop_segment(0.97)  # rest: empty
    assert e - s == 0.0
    # draw end reaches completion continuously
    s, e = loop_segment(0.4199)
    assert e > 0.99
