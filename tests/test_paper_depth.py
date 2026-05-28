import tempfile
from pathlib import Path

import pytest

from analytics.arb import PairQuote
from analytics.paper import PaperTrader
from store.sqlite import Store


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as td:
        s = Store(path=Path(td) / "t.db")
        yield s
        s.close()


def test_maybe_enter_with_depth_persists_fill_vwap(store, monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    monkeypatch.setenv("PAPER_MAX_TTE_DAYS", "0")  # disable TTE filter
    pt = PaperTrader(store, enabled=True)
    pq = PairQuote(pair_name="x")
    cand = {
        "direction": "poly_yes_kalshi_no",
        "net_edge": 0.05,
        "cost": 0.95,
        "poly_leg_price": 0.509,
        "kalshi_leg_price": 0.456,
        "fees": 0.01,
        "fill_vwap_poly": 0.509,
        "fill_vwap_kalshi": 0.456,
        "fill_qty_poly": 100.0,
        "fill_qty_kalshi": 100.0,
        "levels_consumed_poly": 3,
        "levels_consumed_kalshi": 2,
        "partial_fill": False,
    }
    pid = pt.maybe_enter(pq, cand)
    assert pid is not None
    row = store.conn.execute(
        "SELECT entry_fill_vwap_poly, entry_fill_vwap_kalshi, "
        "       entry_levels_consumed_poly, entry_partial_fill, entry_poly_price "
        "FROM paper_positions WHERE id = ?",
        (pid,),
    ).fetchone()
    assert row[0] == pytest.approx(0.509)
    assert row[1] == pytest.approx(0.456)
    assert row[2] == 3
    assert row[3] == 0
    # entry_poly_price also stores the VWAP (legacy field, same semantics now)
    assert row[4] == pytest.approx(0.509)


def test_maybe_enter_rejects_partial_when_required(store, monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_REQUIRE_FULL", "1")
    monkeypatch.setenv("PAPER_MAX_TTE_DAYS", "0")
    pt = PaperTrader(store, enabled=True)
    pq = PairQuote(pair_name="x")
    cand = {
        "direction": "poly_yes_kalshi_no",
        "net_edge": 0.05, "cost": 0.95,
        "poly_leg_price": 0.509, "kalshi_leg_price": 0.456, "fees": 0.01,
        "fill_vwap_poly": 0.509, "fill_vwap_kalshi": 0.456,
        "fill_qty_poly": 10.0, "fill_qty_kalshi": 100.0,  # partial on poly
        "levels_consumed_poly": 1, "levels_consumed_kalshi": 2,
        "partial_fill": True,
    }
    pid = pt.maybe_enter(pq, cand)
    assert pid is None


def test_mark_all_uses_depth_walked_bid_when_available(store, monkeypatch):
    monkeypatch.setenv("FILL_DEPTH_ENABLED", "1")
    monkeypatch.setenv("FILL_SIZE_CONTRACTS", "100")
    monkeypatch.setenv("PAPER_MAX_TTE_DAYS", "0")
    pt = PaperTrader(store, enabled=True)
    # Open a position
    pq_entry = PairQuote(pair_name="x")
    cand = {
        "direction": "poly_yes_kalshi_no",
        "net_edge": 0.05, "cost": 0.95,
        "poly_leg_price": 0.50, "kalshi_leg_price": 0.45, "fees": 0.0,
        "fill_vwap_poly": 0.50, "fill_vwap_kalshi": 0.45,
        "fill_qty_poly": 100.0, "fill_qty_kalshi": 100.0,
        "levels_consumed_poly": 1, "levels_consumed_kalshi": 1, "partial_fill": False,
    }
    pid = pt.maybe_enter(pq_entry, cand)
    # Now mark with a different pq that has bid depth — closing leg = bid side
    pq_mark = PairQuote(
        pair_name="x",
        poly_yes_bid=0.51, poly_yes_ask=0.52,
        poly_yes_bids={0.51: 30.0, 0.50: 100.0},  # walk 100 from top: (30*0.51 + 70*0.50)/100 = 0.503
        kalshi_yes_bid=0.49, kalshi_yes_ask=0.50,
        # For direction A we close Kalshi NO; NO bid depth comes from kalshi.kalshi_no_bids
        kalshi_no_bids={0.48: 200.0},  # walk 100: 0.48
    )
    pt.mark_all([pq_mark])
    row = store.conn.execute(
        "SELECT mark_pnl FROM paper_positions WHERE id = ?", (pid,)
    ).fetchone()
    # Liquidation value @ 100 = 0.503 (poly_yes_bid VWAP) + 0.48 (kalshi_no_bid) = 0.983
    # Entry cost = 0.95, fees = 0 → mark/c = 0.033, mark_pnl = 3.30
    assert row[0] == pytest.approx(3.30, abs=0.01)
