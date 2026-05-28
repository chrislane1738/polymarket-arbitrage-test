import tempfile
from pathlib import Path

from store.sqlite import Store


def test_schema_includes_new_fill_columns():
    with tempfile.TemporaryDirectory() as td:
        s = Store(path=Path(td) / "t.db")
        cur = s.conn.execute("PRAGMA table_info(arb_candidates)")
        cols = {r[1] for r in cur.fetchall()}
        for c in (
            "fill_vwap_poly", "fill_vwap_kalshi",
            "fill_qty_poly", "fill_qty_kalshi",
            "levels_consumed_poly", "levels_consumed_kalshi",
            "partial_fill",
        ):
            assert c in cols, f"arb_candidates missing column {c}"

        cur = s.conn.execute("PRAGMA table_info(paper_positions)")
        cols = {r[1] for r in cur.fetchall()}
        for c in (
            "entry_fill_vwap_poly", "entry_fill_vwap_kalshi",
            "entry_levels_consumed_poly", "entry_levels_consumed_kalshi",
            "entry_partial_fill",
        ):
            assert c in cols, f"paper_positions missing column {c}"
        s.close()


def test_record_arb_candidate_persists_fill_fields():
    with tempfile.TemporaryDirectory() as td:
        s = Store(path=Path(td) / "t.db")

        class FakePQ:
            pair_name = "x"
            poly_yes_bid = poly_yes_ask = poly_no_bid = poly_no_ask = None
            kalshi_yes_bid = kalshi_yes_ask = None
            kalshi_ticker = "T1"

        cand = {
            "direction": "poly_yes_kalshi_no",
            "gross_edge": 0.05, "net_edge": 0.04, "cost": 0.95,
            "poly_leg_price": 0.50, "kalshi_leg_price": 0.45, "fees": 0.01,
            "fill_vwap_poly": 0.509, "fill_vwap_kalshi": 0.456,
            "fill_qty_poly": 100.0, "fill_qty_kalshi": 100.0,
            "levels_consumed_poly": 3, "levels_consumed_kalshi": 2,
            "partial_fill": False,
        }
        s.record_arb_candidate(FakePQ(), cand)
        row = s.conn.execute(
            "SELECT fill_vwap_poly, fill_vwap_kalshi, levels_consumed_poly, partial_fill "
            "FROM arb_candidates ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == 0.509
        assert row[1] == 0.456
        assert row[2] == 3
        assert row[3] == 0  # sqlite stores bool as int
        s.close()
