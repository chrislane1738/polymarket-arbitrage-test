import pytest

from analytics.arb import PairQuote, evaluate


def test_pairquote_accepts_depth_fields_default_none():
    pq = PairQuote(pair_name="x")
    assert pq.poly_yes_asks is None
    assert pq.poly_no_asks is None
    assert pq.kalshi_yes_bids is None
    assert pq.kalshi_no_bids is None


def test_pairquote_populates_depth_fields():
    pq = PairQuote(
        pair_name="x",
        poly_yes_asks={0.50: 100.0},
        poly_no_asks={0.48: 200.0},
        kalshi_yes_bids={0.49: 50.0},
        kalshi_no_bids={0.51: 75.0},
    )
    assert pq.poly_yes_asks == {0.50: 100.0}
    assert pq.kalshi_no_bids == {0.51: 75.0}


def test_evaluate_without_depth_falls_back_to_top_of_book(monkeypatch):
    """Backward compat: when depth dicts are None, behave like before —
    use top-of-book ask, no fill_vwap / levels_consumed fields."""
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")  # gate is on but no depth → fallback
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    pq = PairQuote(
        pair_name="x",
        poly_yes_ask=0.50, poly_yes_ts_ns=1_000_000_000,
        kalshi_yes_bid=0.55, kalshi_yes_ask=0.56, kalshi_ts_ns=1_000_000_000,
    )
    cands = evaluate(pq, now_ns=2_000_000_000)
    a = next(c for c in cands if c["direction"] == "poly_yes_kalshi_no")
    # kalshi_no_ask = 1 - kalshi_yes_bid = 0.45; cost = 0.50 + 0.45 = 0.95
    assert a["cost"] == pytest.approx(0.95)
    assert a["fill_vwap_poly"] == pytest.approx(0.50)
    assert a["fill_vwap_kalshi"] == pytest.approx(0.45)
    assert a["levels_consumed_poly"] == 0
    assert a["levels_consumed_kalshi"] == 0


def test_evaluate_with_depth_walks_book_and_changes_cost(monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    pq = PairQuote(
        pair_name="x",
        poly_yes_ask=0.50, poly_yes_ts_ns=1_000_000_000,
        poly_yes_asks={0.50: 30.0, 0.51: 50.0, 0.52: 100.0},  # walked VWAP for 100 = 0.509
        kalshi_yes_bid=0.55, kalshi_yes_ask=0.56, kalshi_ts_ns=1_000_000_000,
        kalshi_yes_bids={0.55: 40.0, 0.54: 100.0},  # NO ask = 1 - YES bid; walk YES bids to get NO ask VWAP
    )
    cands = evaluate(pq, now_ns=2_000_000_000)
    a = next(c for c in cands if c["direction"] == "poly_yes_kalshi_no")
    # Poly YES ask VWAP @ 100 = (30*0.50 + 50*0.51 + 20*0.52) / 100 = 50.9 / 100 = 0.509
    assert a["fill_vwap_poly"] == pytest.approx(0.509)
    assert a["levels_consumed_poly"] == 3
    # Kalshi NO ask = 1 - kalshi_yes_bid; ask VWAP @ 100 from YES-bid depth
    # YES bids: 0.55:40, 0.54:100 (walk descending — already top-of-book is 0.55)
    # NO asks: 0.45:40, 0.46:100 (walk ascending) — VWAP @ 100 = (40*0.45 + 60*0.46) / 100 = 0.456
    assert a["fill_vwap_kalshi"] == pytest.approx(0.456)
    assert a["levels_consumed_kalshi"] == 2
    # Cost is depth-walked sum
    assert a["cost"] == pytest.approx(0.509 + 0.456)


def test_evaluate_depth_disabled_ignores_depth_dicts(monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "0")
    pq = PairQuote(
        pair_name="x",
        poly_yes_ask=0.50, poly_yes_ts_ns=1_000_000_000,
        poly_yes_asks={0.50: 5.0, 0.99: 1000.0},  # depth would push cost way up
        kalshi_yes_bid=0.55, kalshi_yes_ask=0.56, kalshi_ts_ns=1_000_000_000,
    )
    cands = evaluate(pq, now_ns=2_000_000_000)
    a = next(c for c in cands if c["direction"] == "poly_yes_kalshi_no")
    # With gate off, should match old behavior: top-of-book ask
    assert a["fill_vwap_poly"] == pytest.approx(0.50)
    assert a["levels_consumed_poly"] == 0


def test_evaluate_insufficient_depth_returns_partial_marker(monkeypatch):
    """When walked-fill quantity < requested size, mark candidate as partial.
    We still emit so the user can see it, but add a flag so paper trader can
    refuse if FILL_REQUIRE_FULL=1."""
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    pq = PairQuote(
        pair_name="x",
        poly_yes_ask=0.50, poly_yes_ts_ns=1_000_000_000,
        poly_yes_asks={0.50: 10.0},  # only 10 contracts available, want 100
        kalshi_yes_bid=0.55, kalshi_yes_ask=0.56, kalshi_ts_ns=1_000_000_000,
        kalshi_yes_bids={0.55: 200.0},
    )
    cands = evaluate(pq, now_ns=2_000_000_000)
    a = next(c for c in cands if c["direction"] == "poly_yes_kalshi_no")
    assert a["fill_qty_poly"] == pytest.approx(10.0)
    assert a["fill_qty_kalshi"] == pytest.approx(100.0)
    assert a["partial_fill"] is True
