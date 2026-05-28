from analytics.book import KalshiBookState, PolymarketBookState


def test_polymarket_get_levels_returns_snapshot_copy():
    bs = PolymarketBookState()
    bs.apply({
        "asset_id": "A1",
        "event_type": "book",
        "bids": [{"price": "0.50", "size": "100"}, {"price": "0.49", "size": "50"}],
        "asks": [{"price": "0.52", "size": "30"}, {"price": "0.53", "size": "80"}],
    })
    asks = bs.get_levels("A1", "asks")
    bids = bs.get_levels("A1", "bids")
    assert asks == {0.52: 30.0, 0.53: 80.0}
    assert bids == {0.50: 100.0, 0.49: 50.0}
    # Mutate the returned snapshot — must not affect internal state
    asks[0.52] = 9999.0
    assert bs.get_levels("A1", "asks")[0.52] == 30.0


def test_polymarket_get_levels_unknown_asset_returns_empty():
    bs = PolymarketBookState()
    assert bs.get_levels("nope", "asks") == {}
    assert bs.get_levels("nope", "bids") == {}


def test_kalshi_get_levels_yes_and_no_sides():
    bs = KalshiBookState()
    bs.apply({
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "T1",
            "yes": [[50, 100], [49, 50]],
            "no":  [[48, 200], [47, 75]],
        },
    })
    yes_levels = bs.get_levels("T1", "yes")
    no_levels = bs.get_levels("T1", "no")
    assert yes_levels == {0.50: 100.0, 0.49: 50.0}
    assert no_levels == {0.48: 200.0, 0.47: 75.0}


def test_kalshi_get_levels_unknown_ticker_returns_empty():
    bs = KalshiBookState()
    assert bs.get_levels("nope", "yes") == {}


def test_polymarket_get_levels_invalid_side_raises():
    import pytest
    bs = PolymarketBookState()
    with pytest.raises(ValueError):
        bs.get_levels("A1", "middle")


def test_kalshi_get_levels_invalid_side_raises():
    import pytest
    bs = KalshiBookState()
    with pytest.raises(ValueError):
        bs.get_levels("T1", "middle")


def test_kalshi_get_levels_returns_snapshot_copy():
    bs = KalshiBookState()
    bs.apply({
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": "T1", "yes": [[50, 100]], "no": [[48, 200]]},
    })
    yes = bs.get_levels("T1", "yes")
    yes[0.50] = 9999.0
    assert bs.get_levels("T1", "yes")[0.50] == 100.0
