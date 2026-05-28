import pytest

from analytics.depth import walk_levels


def test_walk_ask_empty_book_returns_no_fill():
    vwap, filled, levels_used = walk_levels({}, qty=10.0, side="ask")
    assert vwap is None
    assert filled == 0.0
    assert levels_used == 0


def test_walk_ask_single_level_partial_fill():
    levels = {0.50: 100.0}
    vwap, filled, levels_used = walk_levels(levels, qty=50.0, side="ask")
    assert vwap == pytest.approx(0.50)
    assert filled == pytest.approx(50.0)
    assert levels_used == 1


def test_walk_ask_walks_ascending_through_multiple_levels():
    levels = {0.52: 100.0, 0.50: 30.0, 0.51: 50.0}
    vwap, filled, levels_used = walk_levels(levels, qty=100.0, side="ask")
    # fills: 30 @ 0.50 + 50 @ 0.51 + 20 @ 0.52 = 15.0 + 25.5 + 10.4 = 50.9
    assert filled == pytest.approx(100.0)
    assert vwap == pytest.approx(50.9 / 100.0)
    assert levels_used == 3


def test_walk_ask_runs_out_of_liquidity_returns_partial():
    levels = {0.50: 20.0}
    vwap, filled, levels_used = walk_levels(levels, qty=100.0, side="ask")
    assert filled == pytest.approx(20.0)
    assert vwap == pytest.approx(0.50)
    assert levels_used == 1


def test_walk_bid_walks_descending_from_top():
    levels = {0.53: 100.0, 0.55: 30.0, 0.54: 50.0}
    vwap, filled, levels_used = walk_levels(levels, qty=100.0, side="bid")
    # fills: 30 @ 0.55 + 50 @ 0.54 + 20 @ 0.53 = 16.5 + 27.0 + 10.6 = 54.1
    assert filled == pytest.approx(100.0)
    assert vwap == pytest.approx(54.1 / 100.0)
    assert levels_used == 3


def test_walk_invalid_side_raises():
    with pytest.raises(ValueError):
        walk_levels({0.50: 10.0}, qty=5.0, side="middle")


def test_walk_zero_qty_returns_no_fill():
    levels = {0.50: 100.0}
    vwap, filled, levels_used = walk_levels(levels, qty=0.0, side="ask")
    assert vwap is None
    assert filled == 0.0
    assert levels_used == 0


def test_walk_bid_runs_out_of_liquidity_returns_partial():
    levels = {0.55: 10.0}
    vwap, filled, levels_used = walk_levels(levels, qty=100.0, side="bid")
    assert filled == pytest.approx(10.0)
    assert vwap == pytest.approx(0.55)
    assert levels_used == 1


def test_walk_negative_qty_returns_no_fill():
    vwap, filled, levels_used = walk_levels({0.50: 100.0}, qty=-5.0, side="ask")
    assert vwap is None
    assert filled == 0.0
    assert levels_used == 0


def test_walk_filters_nan_price_levels():
    levels = {float("nan"): 100.0, 0.50: 30.0, 0.51: 70.0}
    vwap, filled, levels_used = walk_levels(levels, qty=100.0, side="ask")
    # NaN level must be filtered; fill from finite levels only
    # 30 @ 0.50 + 70 @ 0.51 = 15 + 35.7 = 50.7 → VWAP 0.507
    assert filled == pytest.approx(100.0)
    assert vwap == pytest.approx(0.507)
    assert levels_used == 2
