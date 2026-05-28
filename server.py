"""Local FastAPI server for the dashboard.

Run with:
    .venv/bin/uvicorn server:app --reload --port 8000

Read-only against the same SQLite the feeds write to. No auth — bind to
127.0.0.1 only (uvicorn's default).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "feed.db"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="polymarket-arbitrage-test")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _load_pairs() -> list:
    import sys
    sys.path.insert(0, str(ROOT))
    from match.markets import load_pairs
    return load_pairs(ROOT / "config" / "markets.yaml")


def _latest_quote(conn, venue: str, market_id: str):
    row = conn.execute(
        "SELECT best_bid, best_ask, ts_ns FROM quotes "
        "WHERE venue = ? AND market_id = ? ORDER BY ts_ns DESC LIMIT 1",
        (venue, market_id),
    ).fetchone()
    if not row:
        return None
    return dict(row)


@app.get("/api/state")
def state(arb_limit: int = 30, event_limit: int = 30):
    """One-shot snapshot for the dashboard to render."""
    if not DB_PATH.exists():
        return JSONResponse({"error": "no data yet — run scripts.run_feeds first"}, status_code=503)

    now_ns = time.time_ns()
    pairs = _load_pairs()
    spark_n = 40  # last N edge points per (pair, direction) for sparkline
    with db() as conn:
        # Pre-fetch all resolution metadata once
        resolution_meta: dict[str, dict] = {}
        for row in conn.execute(
            "SELECT pair_name, poly_close_ts_ns, kalshi_close_ts_ns, "
            "       poly_result, kalshi_result, poly_result_ts_ns, kalshi_result_ts_ns "
            "FROM pair_resolution"
        ):
            resolution_meta[row[0]] = {
                "poly_close_ts_ns":    row[1],
                "kalshi_close_ts_ns":  row[2],
                "poly_result":         row[3],
                "kalshi_result":       row[4],
                "poly_result_ts_ns":   row[5],
                "kalshi_result_ts_ns": row[6],
            }
        pair_state = []
        for p in pairs:
            # Pull recent edge history for both directions
            edge_history: dict[str, list[dict]] = {}
            for direction in ("poly_yes_kalshi_no", "kalshi_yes_poly_no"):
                rows = list(conn.execute(
                    "SELECT ts_ns, net_edge FROM arb_candidates "
                    "WHERE pair_name = ? AND direction = ? ORDER BY ts_ns DESC LIMIT ?",
                    (p.name, direction, spark_n),
                ))
                edge_history[direction] = [
                    {"ts_ns": r[0], "net_edge": r[1]} for r in reversed(rows)
                ]
            row = {
                "name": p.name,
                "kalshi_ticker": p.kalshi_yes_ticker,
                "poly_yes_token": p.polymarket_yes_token,
                "poly_no_token": p.polymarket_no_token,
                "poly_yes": _latest_quote(conn, "polymarket", p.polymarket_yes_token) if p.polymarket_yes_token else None,
                "poly_no":  _latest_quote(conn, "polymarket", p.polymarket_no_token)  if p.polymarket_no_token  else None,
                "kalshi":   _latest_quote(conn, "kalshi", p.kalshi_yes_ticker)        if p.kalshi_yes_ticker    else None,
            }
            # Compute current edges client-friendly
            edges = {}
            yes_ask = (row["poly_yes"] or {}).get("best_ask")
            no_ask = (row["poly_no"] or {}).get("best_ask")
            k_bid = (row["kalshi"] or {}).get("best_bid")
            k_ask = (row["kalshi"] or {}).get("best_ask")
            if yes_ask is not None and k_bid is not None:
                # Direction A: long poly YES + long kalshi NO. Kalshi NO ask = 1 - k_bid
                cost = yes_ask + (1.0 - k_bid)
                edges["poly_yes_kalshi_no_gross"] = 1.0 - cost
            if no_ask is not None and k_ask is not None:
                # Direction B: long kalshi YES + long poly NO
                cost = k_ask + no_ask
                edges["kalshi_yes_poly_no_gross"] = 1.0 - cost
            row["edges"] = edges
            row["edge_history"] = edge_history
            row["resolution"] = resolution_meta.get(p.name) or {}
            pair_state.append(row)

        # Recent arb candidates
        arbs = [
            dict(r) for r in conn.execute(
                "SELECT ts_ns, pair_name, direction, gross_edge, net_edge, cost, "
                "       poly_leg_price, kalshi_leg_price, fees "
                "FROM arb_candidates ORDER BY ts_ns DESC LIMIT ?",
                (arb_limit,),
            )
        ]

        # Paper positions (all)
        positions = [dict(r) for r in conn.execute(
            "SELECT id, opened_ts_ns, closed_ts_ns, pair_name, direction, size, "
            "       entry_poly_price, entry_kalshi_price, entry_net_edge, entry_fees, "
            "       close_pnl, close_reason, mark_pnl, held_to_expiry_pnl, "
            "       max_adverse_pnl, max_favorable_pnl, "
            "       max_adverse_ts_ns, max_favorable_ts_ns "
            "FROM paper_positions ORDER BY id DESC"
        )]

        # Recent events for the order-flow stream
        events = [dict(r) for r in conn.execute(
            "SELECT ts_ns, venue, event_type, market_id "
            "FROM events ORDER BY ts_ns DESC LIMIT ?",
            (event_limit,),
        )]

        # Counts for footer
        counts = {
            "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "quotes": conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
            "trades": conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
            "arbs":   conn.execute("SELECT COUNT(*) FROM arb_candidates").fetchone()[0],
            "positions": conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0],
            "open_positions": conn.execute("SELECT COUNT(*) FROM paper_positions WHERE closed_ts_ns IS NULL").fetchone()[0],
        }

        # Per-venue feed health: events in the last 60s + age of newest event.
        # Capture a fresh timestamp here — the writer may have inserted events
        # since the top-of-function now_ns, which would otherwise yield negative
        # ages.
        ref_ns = time.time_ns()
        feed_health: dict[str, dict] = {}
        window_start = ref_ns - 60 * 1_000_000_000
        for venue in ("polymarket", "kalshi"):
            rate_row = conn.execute(
                "SELECT COUNT(*) FROM events WHERE venue = ? AND ts_ns > ?",
                (venue, window_start),
            ).fetchone()
            age_row = conn.execute(
                "SELECT MAX(ts_ns) FROM events WHERE venue = ?",
                (venue,),
            ).fetchone()
            last_age = max(0.0, (ref_ns - age_row[0]) / 1e9) if age_row[0] else None
            feed_health[venue] = {
                "events_per_sec": (rate_row[0] / 60.0) if rate_row[0] else 0.0,
                "last_event_age_s": last_age,
            }

        # PnL summary
        rows = conn.execute(
            "SELECT closed_ts_ns, close_pnl, mark_pnl, held_to_expiry_pnl FROM paper_positions"
        ).fetchall()
        pnl = {"realized": 0.0, "mark_open": 0.0, "held_open": 0.0}
        for r in rows:
            if r["closed_ts_ns"] is not None:
                pnl["realized"] += r["close_pnl"] or 0
            else:
                pnl["mark_open"] += r["mark_pnl"] or 0
                pnl["held_open"] += r["held_to_expiry_pnl"] or 0

    # Resolution rollup
    n_resolved_pairs = sum(
        1 for m in resolution_meta.values()
        if m.get("poly_result") and m.get("kalshi_result")
    )
    n_pending_pairs = sum(
        1 for m in resolution_meta.values()
        if (
            (m.get("poly_close_ts_ns") and ref_ns > m["poly_close_ts_ns"] and not m.get("poly_result"))
            or (m.get("kalshi_close_ts_ns") and ref_ns > m["kalshi_close_ts_ns"] and not m.get("kalshi_result"))
        )
    )
    n_resolved_positions = sum(
        1 for p in positions if p.get("close_reason") == "resolved"
    )

    return {
        "now_ns": now_ns,
        "pairs": pair_state,
        "arbs": arbs,
        "positions": positions,
        "events": events,
        "counts": counts,
        "pnl": pnl,
        "feed_health": feed_health,
        "resolutions": {
            "pairs_resolved": n_resolved_pairs,
            "pairs_pending":  n_pending_pairs,
            "positions_resolved": n_resolved_positions,
        },
    }


@app.get("/api/timeseries/{pair_name}/{direction}")
def timeseries(pair_name: str, direction: str, limit: int = 200):
    """Edge timeseries for one (pair, direction) — for sparkline rendering."""
    with db() as conn:
        rows = conn.execute(
            "SELECT ts_ns, net_edge, gross_edge FROM arb_candidates "
            "WHERE pair_name = ? AND direction = ? ORDER BY ts_ns DESC LIMIT ?",
            (pair_name, direction, limit),
        ).fetchall()
    return {"points": [dict(r) for r in reversed(rows)]}


# Mount the static SPA
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return JSONResponse({"error": "static/index.html missing"}, status_code=500)
    return FileResponse(str(index))
