"""Chart geometry (hand-computed expected coordinates)."""

from alphamarket.charts import polyline, ticks


def test_polyline_maps_extremes_to_box_edges():
    # 3 points over 100px: x = 0, 50, 100; min -> bottom (y=40), max -> top (y=0)
    assert polyline([1.0, 3.0, 2.0], 100, 40) == "0.0,40.0 50.0,0.0 100.0,20.0"


def test_polyline_pinned_scale_for_drawdown():
    # hi pinned at 0 so "no drawdown" sits on the top edge regardless of data
    assert polyline([0.0, -0.1, -0.05], 200, 100, lo=-0.2, hi=0.0) == (
        "0.0,0.0 100.0,50.0 200.0,25.0"
    )


def test_polyline_degenerate_series():
    assert polyline([], 100, 40) == ""
    assert polyline([5.0], 100, 40) == "50.0,20.0"  # single point: centred
    assert polyline([2.0, 2.0, 2.0], 90, 40) == "0.0,20.0 45.0,20.0 90.0,20.0"  # flat: mid


def test_ticks_top_to_bottom():
    assert ticks(0.0, 100.0, 5) == [100.0, 75.0, 50.0, 25.0, 0.0]
    assert ticks(1.0, 2.0, 1) == [2.0]
